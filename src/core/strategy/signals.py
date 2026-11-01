"""Stage 3 Step 3 eligibility, risk features and fixed three-module signals."""
import json
from pathlib import Path

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract, parse_ymd
from core.strategy.inputs import SnapshotInputs, quote_name, quote_string

MAIN_FACTORS = ("bp", "sp_ttm", "rev_20", "vol_20")
RISK_FACTORS = ("log_circ_mv", "log_total_mv", "turn_20", "amihud_20", "log_amount_20", "vol_60")
CONTROL_FACTORS = ("composite_equal", "composite_icir_60")
TABLE_KEYS = {
    "strategy_universe": ["trade_date", "ts_code", "universe_id"],
    "strategy_feature": ["trade_date", "ts_code", "universe_id"],
    "strategy_signal": ["trade_date", "ts_code", "strategy_id"],
    "strategy_signal_daily": ["trade_date", "strategy_id"],
}


def build_signal_views(inputs: SnapshotInputs, config: dict, overrides=None, as_of=None):
    overrides = overrides or {}
    rules = dict(config["universe"])
    rules.update(overrides.get("universe", {}))
    cutoff = parse_ymd(as_of or inputs.cutoff)
    if cutoff > parse_ymd(inputs.cutoff):
        raise PermissionError(f"Signal as_of exceeds input cutoff: {cutoff}")
    date_sql = cutoff.isoformat()
    db = inputs.db
    price = inputs.relation("l1/daily_adjusted_price")
    state = inputs.relation("l1/daily_stock_state")
    status = inputs.relation("l1/daily_trade_status")
    universe = inputs.relation("l2/daily_research_universe")
    industry = inputs.relation("l3/daily_industry")
    processed = inputs.relation("l3/factor_processed")
    factor_list = ",".join(quote_string(x) for x in MAIN_FACTORS + RISK_FACTORS + CONTROL_FACTORS)
    accepted_industry = ",".join(quote_string(x) for x in config["universe"]["accepted_industry_statuses"])
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW stage3_price_history AS
        SELECT s.trade_date,s.ts_code,p.open,p.close,p.adj_factor,p.adjusted_close,p.amount,p.total_mv,p.circ_mv,
               p.adjusted_close/NULLIF(lag(p.adjusted_close) OVER (PARTITION BY s.ts_code ORDER BY s.trade_date),0)-1 AS quote_return
        FROM {state} s LEFT JOIN {price} p USING(trade_date,ts_code)
        WHERE s.trade_date<=DATE {quote_string(date_sql)}
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW stage3_price_features AS
        WITH moments AS (
            SELECT *,avg(CASE WHEN amount>0 THEN amount*1000 END) OVER w AS adv20_cny,
                   count(CASE WHEN amount>0 THEN 1 END) OVER w AS adv20_observations,
                   sum(return_fixed) OVER w AS sum_return_fixed,
                   sum(return_fixed*return_fixed) OVER w AS sum_square_fixed,
                   count(return_fixed) OVER w AS return_count
            FROM (SELECT *,round(quote_return*1e12)::HUGEINT AS return_fixed FROM stage3_price_history)
            WINDOW w AS (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
        )
        SELECT *,CASE WHEN return_count>0 THEN round(sqrt(
                   ((sum_square_fixed*return_count-sum_return_fixed*sum_return_fixed)/(return_count*return_count))/1e24),12)
               END AS return_volatility_20
        FROM moments
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW stage3_factor_wide AS
        SELECT trade_date,ts_code,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='bp') AS bp,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='sp_ttm') AS sp_ttm,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='rev_20') AS rev_20,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='vol_20') AS vol_20,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='log_circ_mv') AS log_circ_mv,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='log_total_mv') AS log_total_mv,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='turn_20') AS turn_20,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='amihud_20') AS amihud_20,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='log_amount_20') AS log_amount_20,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='vol_60') AS vol_60,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='composite_equal') AS composite_equal,
               max(TRY_CAST(value AS DOUBLE)) FILTER(WHERE factor_id='composite_icir_60') AS composite_icir_60
        FROM {processed}
        WHERE trade_date<=DATE {quote_string(date_sql)}
          AND universe_id={quote_string(config['signal']['universe_id'])}
          AND process_id={quote_string(config['signal']['process_id'])}
          AND factor_id IN ({factor_list})
        GROUP BY trade_date,ts_code
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW stage3_base AS
        SELECT u.trade_date,u.ts_code,u.universe_id,p.open,p.close,p.adj_factor,p.adjusted_close,
               p.amount,p.total_mv,p.circ_mv,p.adv20_cny,p.adv20_observations,p.return_volatility_20,
               st.is_suspended,st.quotation_exists,st.limit_price_exists,
               st.ts_code IS NOT NULL AS status_known,ss.ts_code IS NOT NULL AS stock_state_known,
               ss.is_st,ss.listing_trade_days,ss.listing_trade_days_lower_bound,
               CASE WHEN i.industry_pit_status IN ({accepted_industry}) AND i.in_date<=u.trade_date
                    THEN coalesce(i.l1_name,{quote_string(config['universe']['unknown_industry_label'])})
                    ELSE {quote_string(config['universe']['unknown_industry_label'])} END AS industry_l1,
               i.industry_pit_status
        FROM {universe} u
        JOIN stage3_price_features p USING(trade_date,ts_code)
        LEFT JOIN {status} st USING(trade_date,ts_code)
        LEFT JOIN {state} ss USING(trade_date,ts_code)
        LEFT JOIN {industry} i USING(trade_date,ts_code)
        WHERE u.trade_date<=DATE {quote_string(date_sql)}
          AND u.universe_id={quote_string(config['universe']['base_id'])}
          AND u.is_in_universe
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW stage3_eligibility AS
        WITH thresholds AS (
            SELECT trade_date,quantile_cont(circ_mv,{float(rules['exclude_bottom_circ_mv_fraction'])}) AS size_floor
            FROM stage3_base GROUP BY trade_date
        ), flagged AS (
            SELECT b.*,t.size_floor,
                   close>0 AND total_mv>0 AND circ_mv>0 AND amount>0 AS quote_market_valid,
                   adv20_observations>={int(rules['min_adv_observations'])} AND adv20_cny>={float(rules['min_adv20_cny'])} AS liquidity_valid,
                   circ_mv>size_floor AS size_valid,
                   status_known AND is_suspended IS FALSE AND stock_state_known
                       AND is_st IS FALSE AND listing_trade_days_lower_bound>={int(rules['listing_trade_days'])} AS state_valid
            FROM stage3_base b JOIN thresholds t USING(trade_date)
        )
        SELECT *,quote_market_valid AND liquidity_valid AND size_valid AND state_valid AS other_eligible
        FROM flagged
    """)
    db.execute("""
        CREATE OR REPLACE TEMP VIEW stage3_scored_base AS
        SELECT e.*,f.bp,f.sp_ttm,f.rev_20,f.vol_20,f.log_circ_mv,f.log_total_mv,f.turn_20,
               f.amihud_20,f.log_amount_20,f.vol_60,f.composite_equal,f.composite_icir_60,
               f.bp IS NOT NULL AND f.sp_ttm IS NOT NULL AND f.rev_20 IS NOT NULL AND f.vol_20 IS NOT NULL AS factors_complete,
               (f.bp+f.sp_ttm)/2 AS value_module,-f.rev_20 AS reversal_module,-f.vol_20 AS low_vol_module,
               ((f.bp+f.sp_ttm)/2-f.rev_20-f.vol_20)/3 AS score
        FROM stage3_eligibility e LEFT JOIN stage3_factor_wide f USING(trade_date,ts_code)
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW stage3_daily AS
        SELECT trade_date,{quote_string(config['signal']['primary_id'])} AS strategy_id,
               count(*) FILTER(WHERE other_eligible) AS other_eligible_count,
               count(*) FILTER(WHERE other_eligible AND factors_complete) AS factor_complete_count,
               count(*) FILTER(WHERE other_eligible AND factors_complete)>= {int(rules['min_cross_section'])} AS cross_section_pass,
               CASE WHEN count(*) FILTER(WHERE other_eligible)>0
                    THEN count(*) FILTER(WHERE other_eligible AND factors_complete)::DOUBLE/count(*) FILTER(WHERE other_eligible) END AS common_factor_coverage,
               min(score) FILTER(WHERE other_eligible AND factors_complete) AS min_score,
               max(score) FILTER(WHERE other_eligible AND factors_complete) AS max_score
        FROM stage3_scored_base GROUP BY trade_date
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP TABLE stage3_gated AS
        SELECT s.*,d.other_eligible_count,d.factor_complete_count,d.cross_section_pass,d.common_factor_coverage,
               d.min_score,d.max_score,
               d.cross_section_pass
                   AND d.common_factor_coverage>={float(rules['min_common_factor_coverage'])}
                   AND d.min_score<d.max_score AS day_gate_pass,
               d.cross_section_pass
                   AND d.common_factor_coverage>={float(rules['min_common_factor_coverage'])}
                   AND d.min_score<d.max_score
                   AND s.other_eligible AND s.factors_complete AS is_signal_eligible
        FROM stage3_scored_base s JOIN stage3_daily d USING(trade_date)
    """)
    db.execute("""
        CREATE OR REPLACE TEMP VIEW strategy_universe AS
        SELECT trade_date,ts_code,universe_id,quote_market_valid,liquidity_valid,size_valid,state_valid,
               other_eligible,factors_complete,day_gate_pass,is_signal_eligible,common_factor_coverage,
               other_eligible_count,factor_complete_count,
               concat_ws(';',
                   CASE WHEN NOT quote_market_valid THEN 'quote_market_invalid' END,
                   CASE WHEN NOT liquidity_valid THEN 'liquidity_invalid' END,
                   CASE WHEN NOT size_valid THEN 'bottom_size_decile' END,
                   CASE WHEN NOT state_valid THEN 'state_unknown_or_ineligible' END,
                   CASE WHEN other_eligible AND NOT factors_complete THEN 'main_factor_missing' END,
                   CASE WHEN other_eligible AND factors_complete AND NOT day_gate_pass THEN 'daily_gate_closed' END
               ) AS exclusion_reason
        FROM stage3_gated
    """)
    db.execute("""
        CREATE OR REPLACE TEMP VIEW strategy_feature AS
        SELECT trade_date,ts_code,universe_id,close,adj_factor,adjusted_close,amount,total_mv,circ_mv,
               adv20_cny,adv20_observations,return_volatility_20,log_circ_mv,log_total_mv,turn_20,
               amihud_20,log_amount_20,vol_60,industry_l1,industry_pit_status,size_floor,
               listing_trade_days,listing_trade_days_lower_bound
        FROM stage3_gated
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW strategy_signal AS
        SELECT trade_date,ts_code,{quote_string(config['signal']['primary_id'])} AS strategy_id,
               value_module,reversal_module,low_vol_module,score,
               CASE WHEN is_signal_eligible THEN row_number() OVER(
                   PARTITION BY trade_date ORDER BY CASE WHEN is_signal_eligible THEN score END DESC,ts_code) END AS score_rank,
               factors_complete,is_signal_eligible,day_gate_pass,common_factor_coverage,
               composite_equal,composite_icir_60
        FROM stage3_gated
    """)
    db.execute(f"""
        CREATE OR REPLACE TEMP VIEW strategy_signal_daily AS
        SELECT DISTINCT trade_date,{quote_string(config['signal']['primary_id'])} AS strategy_id,
               other_eligible_count,factor_complete_count,cross_section_pass,common_factor_coverage,
               min_score,max_score,min_score<max_score AS nondegenerate_score,day_gate_pass
        FROM stage3_gated
    """)
    return {"tables": list(TABLE_KEYS), "as_of": date_sql, "read_log": list(inputs.read_log)}


def publish_signals(inputs: SnapshotInputs, output_root, config: dict, overrides=None, as_of=None, run_id=None):
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Run output is immutable: {output_root}")
    output_root.mkdir(parents=True)
    build = build_signal_views(inputs, config, overrides, as_of)
    if "l3/forward_return" in inputs.read_log:
        raise PermissionError("Signal build attempted to read research labels")
    manifests = {}
    for table, key in TABLE_KEYS.items():
        target = output_root / "l3" / table / "part-00000.parquet"
        target.parent.mkdir(parents=True)
        relation = f"read_parquet({quote_string(str(target))})"
        order = ",".join(quote_name(field) for field in key)
        inputs.db.execute(f"COPY (SELECT * FROM {table} ORDER BY {order}) TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
        count, distinct = inputs.db.execute(f"SELECT count(*),count(DISTINCT ROW({order})) FROM {relation}").fetchone()
        nulls = inputs.db.execute(f"SELECT count(*) FROM {relation} WHERE {' OR '.join(f'{quote_name(field)} IS NULL' for field in key)}").fetchone()[0]
        if count != distinct or nulls:
            raise ValueError(f"Invalid published key: {table}")
        schema = {row[0]: row[1] for row in inputs.db.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}
        manifests[table] = {"path": f"l3/{table}/part-00000.parquet", "primary_key": key,
                            "rows": count, "schema": schema, "sha256": file_sha256(target)}
        (target.parent / "manifest.json").write_text(json.dumps(manifests[table], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    identity = {"step": 3, "run_id": run_id or output_root.name, "as_of": build["as_of"],
                "input_bundle": inputs.manifest.get("bundle_id", "golden_stage3"),
                "input_read_log": inputs.read_log, "research_labels_read": False,
                "tables": manifests, "overrides": overrides or {}}
    (output_root / "manifest.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return identity


def default_run_id(project_root=PROJECT_ROOT):
    contract = load_stage3_contract(project_root)
    active = contract["config"]["delivery"].get("active_step3_run")
    if active:
        return active
    bundle = contract["config"]["delivery"]["active_input_bundle"]
    manifest_path = project_root / "data_middle/strategy_research/inputs" / bundle / "manifest.json"
    identity = {"step": 3, "bundle_manifest": file_sha256(manifest_path),
                "config": contract["config_sha256"], "signals": file_sha256(Path(__file__))}
    return "step3_" + canonical_hash(identity)[:16]
