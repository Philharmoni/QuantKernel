"""Baseline factor library and the L3 factor_value publication (plan Step 5).

Stage 2 source directory: only L1 products are consumed, never raw tables and
never core.label (enforced by core.factor.entry guards).

Formulas live in configs/factor_registry.yaml; this module is their execution
engine. Two input-cutoff parameters are deliberately separate:

  start/end   restrict the OUTPUT dates only (rolling windows still see the
              full lookback history before start);
  input_end   restrict every INPUT to <= this date. The truncation-invariance
              gate (core.factor.truncate) rebuilds with input_end=D and
              start=end=D and requires the value at D to be bit-identical to
              the full build -- this is the anti-look-ahead proof, not a
              performance option.

Only non-NULL factor values are published: absence of a row means "not
computable at that date" (insufficient history, unavailable financials), which
is what the per-day coverage report measures.
"""
from core.config.settings import load_settings
from core.data.store import qs
from core.factor.asof import (balance_versions_daily, current_metric_daily,
                              metric_versions_daily)
from core.factor.registry import factor_map
from core.market.prices import date_window

PRICE_FACTORS = ("log_total_mv", "log_circ_mv", "mom_12_1", "mom_6_1", "rev_20",
                 "vol_20", "vol_60", "turn_20", "amihud_20", "log_amount_20")


def _price_input(store, input_end):
    prices = store.output_relation("l1", "daily_adjusted_price")
    calendar = store.output_relation("l1", "daily_calendar")
    cutoff = f"WHERE p.trade_date<=DATE {qs(input_end)}" if input_end else ""
    # Materialized once: the rolling windows, the momentum self-joins and every
    # financial join below all reuse this same bounded input.
    store.db.execute(f"""CREATE OR REPLACE TEMP TABLE factor_price_input AS
        SELECT p.trade_date, p.ts_code, c.trade_index,
               p.adjusted_close, p.ret_1d,
               try_cast(p.total_mv AS DOUBLE) AS total_mv,
               try_cast(p.circ_mv AS DOUBLE) AS circ_mv,
               try_cast(p.turnover_rate AS DOUBLE) AS turnover_rate,
               p.amount
        FROM {prices} p JOIN {calendar} c USING(trade_date)
        {cutoff}
    """)


def _build_price_factors(store, selected, start, end):
    window = date_window(store, start, end)
    # Deterministic rolling aggregates (same convention as Step 4's benchmark
    # returns and the frozen repeatability.preferred_method): DuckDB's
    # stddev_pop/avg window functions accumulate in execution order, so the
    # full-history build and a truncated rebuild can differ in the last ulp.
    # Exact DECIMAL sums are order-independent; only the final quotient/sqrt is
    # done in DOUBLE. Scales are chosen per metric so tiny Amihud values
    # (~1e-12) keep full relative precision.
    store.db.execute("""CREATE OR REPLACE TEMP VIEW factor_price_windows AS
        SELECT *,
               count(ret_1d) OVER w20 AS ret_n20,
               sum(cast(ret_1d AS DECIMAL(38,24))) OVER w20 AS ret_s1_20,
               sum(cast(ret_1d*ret_1d AS DECIMAL(38,24))) OVER w20 AS ret_s2_20,
               count(ret_1d) OVER w60 AS ret_n60,
               sum(cast(ret_1d AS DECIMAL(38,24))) OVER w60 AS ret_s1_60,
               sum(cast(ret_1d*ret_1d AS DECIMAL(38,24))) OVER w60 AS ret_s2_60,
               count(turnover_rate) OVER w20 AS turn_n20,
               sum(cast(turnover_rate AS DECIMAL(38,18))) OVER w20 AS turn_s20,
               count(CASE WHEN ret_1d IS NOT NULL AND amount>0 THEN 1 END) OVER w20 AS amihud_n20,
               sum(cast(CASE WHEN amount>0 THEN abs(ret_1d)/(amount*1000) END
                        AS DECIMAL(38,24))) OVER w20 AS amihud_s20,
               count(amount) OVER w20 AS amount_n20,
               sum(cast(amount*1000 AS DECIMAL(38,18))) OVER w20 AS amount_s20
        FROM factor_price_input
        WINDOW w20 AS (PARTITION BY ts_code ORDER BY trade_index
                       RANGE BETWEEN 19 PRECEDING AND CURRENT ROW),
               w60 AS (PARTITION BY ts_code ORDER BY trade_index
                       RANGE BETWEEN 59 PRECEDING AND CURRENT ROW)
    """)
    simple = {
        "log_total_mv": ("ln(total_mv*10000)", "total_mv>0 AND isfinite(total_mv)"),
        "log_circ_mv": ("ln(circ_mv*10000)", "circ_mv>0 AND isfinite(circ_mv)"),
        "vol_20": ("sqrt(greatest(cast(ret_s2_20 as DOUBLE)/ret_n20"
                   "-pow(cast(ret_s1_20 as DOUBLE)/ret_n20,2),0))", "ret_n20>=20"),
        "vol_60": ("sqrt(greatest(cast(ret_s2_60 as DOUBLE)/ret_n60"
                   "-pow(cast(ret_s1_60 as DOUBLE)/ret_n60,2),0))", "ret_n60>=60"),
        "turn_20": ("cast(turn_s20 as DOUBLE)/turn_n20", "turn_n20>=20"),
        "amihud_20": ("cast(amihud_s20 as DOUBLE)/amihud_n20", "amihud_n20>=20"),
        "log_amount_20": ("ln(cast(amount_s20 as DOUBLE)/amount_n20)",
                          "amount_n20>=20 AND amount_s20>0"),
    }
    for factor_id, (expression, condition) in simple.items():
        if factor_id not in selected:
            continue
        store.db.execute(f"""INSERT INTO factor_parts
            SELECT trade_date, ts_code, '{factor_id}', {expression}
            FROM factor_price_windows
            WHERE {condition} AND isfinite({expression}) AND {window}
        """)
    momentum = {"mom_12_1", "mom_6_1", "rev_20"} & set(selected)
    if momentum:
        store.db.execute("""CREATE OR REPLACE TEMP VIEW factor_momentum AS
            SELECT cur.trade_date, cur.ts_code,
                   cur.adjusted_close/prev20.adjusted_close-1 AS rev_20,
                   skip21.adjusted_close/base252.adjusted_close-1 AS mom_12_1,
                   skip21.adjusted_close/base126.adjusted_close-1 AS mom_6_1
            FROM factor_price_input cur
            LEFT JOIN factor_price_input prev20
              ON prev20.ts_code=cur.ts_code AND prev20.trade_index=cur.trade_index-20
            LEFT JOIN factor_price_input skip21
              ON skip21.ts_code=cur.ts_code AND skip21.trade_index=cur.trade_index-21
            LEFT JOIN factor_price_input base252
              ON base252.ts_code=cur.ts_code AND base252.trade_index=cur.trade_index-252
            LEFT JOIN factor_price_input base126
              ON base126.ts_code=cur.ts_code AND base126.trade_index=cur.trade_index-126
        """)
        for factor_id in sorted(momentum):
            store.db.execute(f"""INSERT INTO factor_parts
                SELECT trade_date, ts_code, '{factor_id}', {factor_id}
                FROM factor_momentum
                WHERE {factor_id} IS NOT NULL AND isfinite({factor_id}) AND {window}
            """)


def _ttm_current(store, metric, input_end, start, end):
    view = f"factor_ttm_{metric}"
    metric_versions_daily(store, view + "_versions", "ttm_financial", metric, input_end, start, end)
    current_metric_daily(store, view, view + "_versions")
    return view


def _build_value_factors(store, selected, input_end, start, end):
    for factor_id, metric in (("ep_ttm", "n_income_attr_p"), ("sp_ttm", "revenue"),
                              ("cfp_ttm", "n_cashflow_act")):
        if factor_id not in selected:
            continue
        current = _ttm_current(store, metric, input_end, start, end)
        store.db.execute(f"""INSERT INTO factor_parts
            SELECT m.trade_date, m.ts_code, '{factor_id}', m.value/(p.total_mv*10000)
            FROM {current} m JOIN factor_price_input p
              ON p.trade_date=m.trade_date AND p.ts_code=m.ts_code
            WHERE m.status='available' AND m.value IS NOT NULL AND isfinite(m.value)
              AND p.total_mv>0
        """)


def _build_balance_factors(store, selected, input_end, start, end):
    needed = {"bp", "roa_ttm", "roe_ttm"} & set(selected)
    if not needed:
        return
    balance_versions_daily(store, "factor_balance", input_end, start, end)
    if "bp" in needed:
        store.db.execute("""INSERT INTO factor_parts
            SELECT b.trade_date, b.ts_code, 'bp', b.total_hldr_eqy_exc_min_int/(p.total_mv*10000)
            FROM factor_balance_current b JOIN factor_price_input p
              ON p.trade_date=b.trade_date AND p.ts_code=b.ts_code
            WHERE b.total_hldr_eqy_exc_min_int IS NOT NULL AND p.total_mv>0
        """)


def _build_growth_factors(store, selected, input_end, start, end):
    for factor_id, metric in (("rev_yoy_q", "revenue"), ("np_yoy_q", "n_income_attr_p")):
        if factor_id not in selected:
            continue
        view = f"factor_quarterly_{metric}"
        metric_versions_daily(store, view + "_versions", "quarterly_financial", metric,
                              input_end, start, end)
        current_metric_daily(store, view, view + "_versions")
        store.db.execute(f"""INSERT INTO factor_parts
            SELECT cur.trade_date, cur.ts_code, '{factor_id}', cur.value/prev.value-1
            FROM {view} cur JOIN {view}_versions prev
              ON prev.trade_date=cur.trade_date AND prev.ts_code=cur.ts_code
             AND prev.end_date=cur.end_date-INTERVAL 1 YEAR
            WHERE cur.status='available' AND prev.status='available'
              AND cur.value IS NOT NULL AND prev.value IS NOT NULL
              AND prev.value<>0 AND isfinite(cur.value/prev.value-1)
        """)


def _build_quality_factors(store, selected, input_end, start, end):
    needed = {"roa_ttm", "roe_ttm"} & set(selected)
    if not needed:
        return
    current = _ttm_current(store, "n_income_attr_p", input_end, start, end)
    denominators = {"roa_ttm": "total_assets", "roe_ttm": "total_hldr_eqy_exc_min_int"}
    for factor_id in sorted(needed):
        field = denominators[factor_id]
        store.db.execute(f"""INSERT INTO factor_parts
            SELECT m.trade_date, m.ts_code, '{factor_id}', m.value/b.{field}
            FROM {current} m JOIN factor_balance_current b
              ON b.trade_date=m.trade_date AND b.ts_code=m.ts_code
            WHERE m.status='available' AND m.value IS NOT NULL
              AND b.{field} IS NOT NULL AND b.{field}<>0
              AND isfinite(m.value/b.{field})
        """)


def compute_factor_parts(store, start=None, end=None, input_end=None,
                         factor_ids=None, config_root=None) -> str:
    """Fill and return the factor_parts temp table without publishing, so the
    truncation gate can recompute into scratch space instead of clobbering the
    published factor_value artifact."""
    registry = factor_map(config_root)
    engine = load_settings("factor_research.yaml", config_root)["engine"]
    store.db.execute(f"SET memory_limit={qs(engine['memory_limit'])}")
    store.db.execute(f"SET threads={int(engine['threads'])}")
    selected = (list(factor_ids) if factor_ids is not None
                else [factor_id for factor_id, spec in registry.items()
                      if spec["category"] != "composite"])
    unknown = sorted(set(selected) - set(registry))
    if unknown:
        raise ValueError(f"Unregistered factor_ids: {unknown}")
    composites = [factor_id for factor_id in selected
                  if registry[factor_id]["category"] == "composite"]
    if composites:
        raise ValueError(f"Composite factors use core.evaluation.composite: {composites}")
    store.db.execute("CREATE OR REPLACE TEMP TABLE factor_parts("
                     "trade_date DATE, ts_code VARCHAR, factor_id VARCHAR, value DOUBLE)")
    _price_input(store, input_end)
    price_selected = [f for f in selected if f in PRICE_FACTORS]
    if price_selected:
        _build_price_factors(store, price_selected, start, end)
    _build_value_factors(store, selected, input_end, start, end)
    _build_balance_factors(store, selected, input_end, start, end)
    _build_growth_factors(store, selected, input_end, start, end)
    _build_quality_factors(store, selected, input_end, start, end)
    for factor_id in selected:
        # Registry contract: no value may appear before the factor's valid_from.
        store.db.execute(f"DELETE FROM factor_parts WHERE factor_id={qs(factor_id)} "
                         f"AND trade_date<strptime({qs(registry[factor_id]['valid_from'])},'%Y%m%d')::DATE")
    # Free the 9M-row market staging table before the partitioned publish.
    store.db.execute("DROP TABLE IF EXISTS factor_price_input")
    return "factor_parts"


def build_factor_values(store, start=None, end=None, input_end=None,
                        factor_ids=None, config_root=None) -> dict:
    compute_factor_parts(store, start, end, input_end, factor_ids, config_root)
    return store.publish("l3", "factor_value", "SELECT * FROM factor_parts",
                         ["trade_date", "ts_code", "factor_id"], partition_by="factor_id")
