"""Build Step 4 L4 target/order tables from admitted Step 3 signals."""
import json
from pathlib import Path

from core.strategy.contract import file_sha256
from core.strategy.inputs import quote_string
from core.strategy.portfolio import (TABLE_KEYS, make_orders, reference_profile,
                                     select_with_buffer, solve_targets)


def fetch_dicts(db, query, params=None):
    cursor = db.execute(query, params or [])
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


class Step4Builder:
    def __init__(self, inputs, signal_run, config):
        self.inputs = inputs
        self.db = inputs.db
        self.config = config
        self.signal = f"read_parquet({quote_string(str(Path(signal_run) / 'l3/strategy_signal/part-00000.parquet'))})"
        self.universe = f"read_parquet({quote_string(str(Path(signal_run) / 'l3/strategy_universe/part-00000.parquet'))})"
        self.feature = f"read_parquet({quote_string(str(Path(signal_run) / 'l3/strategy_feature/part-00000.parquet'))})"
        self.daily = f"read_parquet({quote_string(str(Path(signal_run) / 'l3/strategy_signal_daily/part-00000.parquet'))})"
        self.price = inputs.relation("l1/daily_adjusted_price")
        self.state = inputs.relation("l1/daily_stock_state")
        self.status = inputs.relation("l1/daily_trade_status")
        self.calendar = inputs.calendar()
        self.nav = float(config["objective"]["initial_capital_cny"])

    def schedule(self):
        start = self.config["research_protocol"]["design_period"][0]
        dates = [day for day in self.calendar.dates if day >= parse_date(start)]
        return dates[::int(self.config["rebalance"]["trading_day_interval"])]

    def day_inputs(self, day, current_codes):
        daily = self.db.execute(f"SELECT day_gate_pass FROM {self.daily} WHERE trade_date=?", [day]).fetchone()
        candidates = fetch_dicts(self.db, f"""SELECT s.score,s.score_rank,f.circ_mv,f.log_circ_mv,f.industry_l1,
            f.adv20_cny,f.return_volatility_20,f.ts_code FROM {self.signal} s JOIN {self.feature} f
            ON s.trade_date=f.trade_date AND s.ts_code=f.ts_code
            WHERE s.trade_date=? AND s.is_signal_eligible ORDER BY s.score_rank,s.ts_code""", [day])
        reference = fetch_dicts(self.db, f"""SELECT f.circ_mv,f.log_circ_mv,f.industry_l1,f.ts_code FROM {self.universe} u
            JOIN {self.feature} f ON u.trade_date=f.trade_date AND u.ts_code=f.ts_code
            WHERE u.trade_date=? AND u.other_eligible ORDER BY f.ts_code""", [day])
        previous = self.previous_inputs(day, current_codes) if current_codes else {}
        return bool(daily and daily[0]), candidates, reference, previous

    def previous_inputs(self, day, codes):
        values = ",".join(quote_string(code) for code in sorted(codes))
        return {row["ts_code"]: row for row in fetch_dicts(self.db, f"""
            WITH history AS (
                SELECT s.trade_date,s.ts_code,p.amount,p.adjusted_close,
                       p.adjusted_close/NULLIF(lag(p.adjusted_close) OVER(PARTITION BY s.ts_code ORDER BY s.trade_date),0)-1 AS quote_return
                FROM {self.state} s LEFT JOIN {self.price} p USING(trade_date,ts_code)
                WHERE s.trade_date<=? AND s.ts_code IN ({values})
            ), moments AS (
                SELECT *,avg(CASE WHEN amount>0 THEN amount*1000 END) OVER w AS adv20_cny,
                       sum(round(quote_return*1e12)::HUGEINT) OVER w AS sum_return,
                       sum(round(quote_return*1e12)::HUGEINT*round(quote_return*1e12)::HUGEINT) OVER w AS sum_square,
                       count(round(quote_return*1e12)::HUGEINT) OVER w AS return_count
                FROM history WINDOW w AS (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
            ), current AS (
                SELECT trade_date,ts_code,adv20_cny,
                       CASE WHEN return_count>0 THEN round(sqrt(((sum_square*return_count-sum_return*sum_return)/(return_count*return_count))/1e24),12)
                       END AS return_volatility_20
                FROM moments WHERE trade_date=?
            )
            SELECT c.ts_code,c.adv20_cny,c.return_volatility_20,ss.is_st,ss.is_delisted,ss.is_listed,
                   st.is_suspended,st.quotation_exists,st.limit_price_exists,
                   f.industry_l1,f.log_circ_mv,f.circ_mv,
                   ss.is_st OR ss.is_delisted OR NOT ss.is_listed AS risk_exit,
                   ss.ts_code IS NULL OR st.ts_code IS NULL OR st.is_suspended IS NOT FALSE
                       OR st.quotation_exists IS NOT TRUE OR st.limit_price_exists IS NOT TRUE AS blocked_sell
            FROM current c LEFT JOIN {self.state} ss USING(trade_date,ts_code)
            LEFT JOIN {self.status} st USING(trade_date,ts_code)
            LEFT JOIN {self.feature} f USING(trade_date,ts_code)
        """, [day, day])}

    def build(self, schedule_limit=None):
        current = {}
        targets, orders, summaries = [], [], []
        for day in self.schedule()[:schedule_limit]:
            open_gate, candidates, reference_rows, previous = self.day_inputs(day, current)
            infos = {row["ts_code"]: dict(row) for row in candidates}
            infos.update(previous)
            frozen = {code: weight for code, weight in current.items() if previous.get(code, {}).get("blocked_sell")}
            risk_exits = {code for code in current if code not in frozen and previous.get(code, {}).get("risk_exit")}
            for code in risk_exits:
                infos[code]["risk_exit"] = True
            if open_gate:
                selected = select_with_buffer(candidates, current, int(self.config["rebalance"]["target_count"]),
                                              int(self.config["rebalance"]["retain_rank_through"]))
                selected_codes = {row["ts_code"] for row in selected}
                ordinary_exits = {code for code in current if code not in frozen and code not in risk_exits and code not in selected_codes}
                try:
                    reference = reference_profile(reference_rows)
                    result = solve_targets(selected, current, frozen, reference, self.config, self.nav,
                                           ordinary_exits, risk_exits, previous)
                    target = result["targets"]
                    status = result["status"]
                except (ValueError, TypeError) as error:
                    target = dict(current)
                    for code in risk_exits:
                        target[code] = 0.0
                    result = {"solver_cycles": 0, "max_violation": None, "variable_count": 0}
                    status = "reference_unavailable:" + str(error)
            else:
                selected = []
                target = dict(current)
                for code in risk_exits:
                    target[code] = 0.0
                result = {"solver_cycles": 0, "max_violation": 0.0, "variable_count": 0}
                status = "daily_gate_closed"
            try:
                execution_date = self.calendar.next(day)
            except PermissionError:
                execution_date = None
                status = "execution_beyond_permission"
            day_orders = make_orders(target, current, infos, self.config, day, execution_date, self.nav)
            selected_codes = {row["ts_code"] for row in selected}
            for code in sorted(set(current) | set(target)):
                info = infos.get(code, {})
                targets.append({"signal_date": day, "ts_code": code,
                    "strategy_id": self.config["signal"]["primary_id"],
                    "current_reference_weight": current.get(code, 0.0), "target_weight": target.get(code, 0.0),
                    "is_frozen": code in frozen, "is_risk_exit": code in risk_exits,
                    "buffer_retained": code in current and code in selected_codes,
                    "score": info.get("score"), "score_rank": info.get("score_rank"),
                    "industry_l1": info.get("industry_l1"), "adv20_cny": info.get("adv20_cny"),
                    "target_reason": "frozen" if code in frozen else "risk_exit" if code in risk_exits else "selected" if target.get(code, 0) > 0 else "exit_or_zero"})
            orders.extend(day_orders)
            summaries.append({"signal_date": day, "strategy_id": self.config["signal"]["primary_id"],
                "planned_execution_date": execution_date, "status": status, "day_gate_pass": open_gate,
                "reference_nav_cny": self.nav, "stock_target_weight": sum(target.values()),
                "cash_reference_weight": 1 - sum(target.values()),
                "target_position_count": sum(weight > 1e-12 for weight in target.values()),
                "frozen_weight": sum(frozen.values()), "risk_exit_count": len(risk_exits),
                "order_count": len(day_orders), "blocked_order_count": sum(row["order_status"] == "blocked" for row in day_orders),
                "reference_one_way_turnover": sum(abs(target.get(code, 0) - current.get(code, 0))
                                                   for code in set(target) | set(current)) / 2,
                "solver_cycles": result["solver_cycles"], "max_constraint_violation": result["max_violation"],
                "solver_variable_count": result["variable_count"],
                "constraint_diagnostics_json": json.dumps(result.get("constraint_diagnostics", {}), sort_keys=True),
                "exposure_diagnostics_json": json.dumps(result.get("exposure_diagnostics", {}), sort_keys=True)})
            current = {code: weight for code, weight in target.items() if weight > 1e-12}
        return {"target_portfolio": targets, "order_intent": orders, "portfolio_daily": summaries}


def parse_date(value):
    from core.strategy.contract import parse_ymd
    return parse_ymd(value)


def publish_step4(builder, output_root, run_id):
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 4 output: {output_root}")
    output_root.mkdir(parents=True)
    tables = builder.build()
    if "l3/forward_return" in builder.inputs.read_log:
        raise PermissionError("Step 4 attempted to read research labels")
    manifests = {}
    import pandas as pd
    for table, rows in tables.items():
        target = output_root / "l4" / table / "part-00000.parquet"
        target.parent.mkdir(parents=True)
        key = TABLE_KEYS[table]
        if rows:
            builder.inputs.db.register("stage3_publish_rows", pd.DataFrame(rows))
            builder.inputs.db.execute(f"COPY (SELECT * FROM stage3_publish_rows ORDER BY {','.join(key)}) TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            builder.inputs.db.unregister("stage3_publish_rows")
        elif table == "target_portfolio":
            builder.inputs.db.execute(f"""COPY (SELECT CAST(NULL AS DATE) signal_date,CAST(NULL AS VARCHAR) ts_code,
                CAST(NULL AS VARCHAR) strategy_id,CAST(NULL AS DOUBLE) current_reference_weight,
                CAST(NULL AS DOUBLE) target_weight,CAST(NULL AS BOOLEAN) is_frozen,CAST(NULL AS BOOLEAN) is_risk_exit,
                CAST(NULL AS BOOLEAN) buffer_retained,CAST(NULL AS DOUBLE) score,CAST(NULL AS BIGINT) score_rank,
                CAST(NULL AS VARCHAR) industry_l1,CAST(NULL AS DOUBLE) adv20_cny,CAST(NULL AS VARCHAR) target_reason
                WHERE FALSE) TO {quote_string(str(target))} (FORMAT PARQUET)""")
        elif table == "order_intent":
            builder.inputs.db.execute(f"""COPY (SELECT CAST(NULL AS VARCHAR) order_id,CAST(NULL AS DATE) signal_date,
                CAST(NULL AS DATE) planned_execution_date,CAST(NULL AS VARCHAR) ts_code,CAST(NULL AS VARCHAR) strategy_id,
                CAST(NULL AS VARCHAR) side,CAST(NULL AS VARCHAR) purpose,CAST(NULL AS DOUBLE) target_weight,
                CAST(NULL AS DOUBLE) current_reference_weight,CAST(NULL AS DOUBLE) order_weight,
                CAST(NULL AS DOUBLE) reference_nav_cny,CAST(NULL AS DOUBLE) reference_notional_cny,
                CAST(NULL AS DOUBLE) adv20_cny,CAST(NULL AS DOUBLE) return_volatility_20,
                CAST(NULL AS DOUBLE) estimated_cost_cny,CAST(NULL AS VARCHAR) time_in_force,
                CAST(NULL AS BOOLEAN) replacement_allowed,CAST(NULL AS VARCHAR) order_status,CAST(NULL AS VARCHAR) block_reason
                WHERE FALSE) TO {quote_string(str(target))} (FORMAT PARQUET)""")
        else:
            raise ValueError(f"Step 4 table is empty: {table}")
        published = f"read_parquet({quote_string(str(target))})"
        count, distinct = builder.inputs.db.execute(f"SELECT count(*),count(DISTINCT ROW({','.join(key)})) FROM {published}").fetchone()
        nulls = builder.inputs.db.execute(f"SELECT count(*) FROM {published} WHERE {' OR '.join(f'{field} IS NULL' for field in key)}").fetchone()[0]
        if count != distinct or nulls:
            raise ValueError(f"Duplicate or null Step 4 key: {table}")
        schema = {row[0]: row[1] for row in builder.inputs.db.execute(f"DESCRIBE SELECT * FROM {published}").fetchall()}
        manifests[table] = {"path": f"l4/{table}/part-00000.parquet", "primary_key": key,
                            "rows": count, "schema": schema, "sha256": file_sha256(target)}
        (target.parent / "manifest.json").write_text(json.dumps(manifests[table], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    identity = {"step": 4, "run_id": run_id, "input_bundle": builder.inputs.manifest["bundle_id"],
                "reference_state": "previous_target_weights_not_actual_fills", "research_labels_read": False,
                "input_read_log": builder.inputs.read_log, "tables": manifests}
    (output_root / "manifest.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return identity
