"""Step 7 design-period research built from independent strategy and passive accounts."""
import copy
import json
from pathlib import Path

from core.strategy.benchmark import relative_metrics
from core.strategy.benchmark_build import Step6Builder, fetch_dicts
from core.strategy.contract import parse_ymd
from core.strategy.inputs import quote_string
from core.strategy.research import extended_performance, monthly_returns, segment_definitions


class Step7Builder:
    def __init__(self, inputs, step3_run, step4_run, step5_run, config,
                 period_id="design", period_range=None, account_start=None):
        self.inputs = inputs
        self.db = inputs.db
        self.config = config
        self.period_id = period_id
        self.step3_run, self.step4_run, self.step5_run = Path(step3_run), Path(step4_run), Path(step5_run)
        period_range = period_range or config["research_protocol"]["design_period"]
        self.start = parse_ymd(period_range[0])
        self.end = parse_ymd(period_range[1])
        self.account_start = parse_ymd(account_start) if account_start else self.start
        self.initial_capital = float(config["objective"]["initial_capital_cny"])
        account_config = copy.deepcopy(config)
        account_config["step6"]["small_window"] = [self.account_start.strftime("%Y%m%d"), period_range[1]]
        self.accounts = Step6Builder(inputs, step3_run, step4_run, step5_run, account_config,
                                     period_id=period_id, nav_only=True)

    def build(self):
        tables = self.accounts.build()
        nav_groups = {}
        for row in tables["account_nav_daily"]:
            nav_groups.setdefault(row["account_id"], []).append(row)
        performance, monthly = [], []
        segments = segment_definitions(self.start, self.end, self.period_id)
        annualization = int(self.config["research_protocol"]["annualization_days"])
        for account_id, rows in sorted(nav_groups.items()):
            rows = sorted(rows, key=lambda row: row["trade_date"])
            for period_id, start, end in segments:
                selected = [row for row in rows if start <= row["trade_date"] <= end]
                prior = [row for row in rows if row["trade_date"] < start]
                baseline = float(prior[-1]["reference_nav_cny"]) if prior else self.initial_capital
                performance.append({"period_id": period_id, "account_id": account_id, "scenario_id": "main",
                    "baseline_nav_cny": baseline, **extended_performance(selected, baseline, annualization)})
            for row in monthly_returns(rows, self.initial_capital):
                if self.start.strftime("%Y-%m") <= row["period_id"] <= self.end.strftime("%Y-%m"):
                    monthly.append({"account_id": account_id, "scenario_id": "main", **row})
        tables["performance"] = performance
        tables["monthly_return"] = monthly
        tables["account_nav_daily"] = [row for row in tables["account_nav_daily"]
                                       if self.start <= row["trade_date"] <= self.end]
        tables["benchmark_target"] = [row for row in tables["benchmark_target"]
                                      if self.start <= row["planned_execution_date"] <= self.end]
        strategy = [row for row in nav_groups["c0_three_module_fixed__executed_proxy_net"]
                    if self.start <= row["trade_date"] <= self.end]
        tables["relative_performance"] = []
        for family in ("eligible_passive", "common_support_passive"):
            benchmark = [row for row in nav_groups[family + "__executed_proxy_net"]
                         if self.start <= row["trade_date"] <= self.end]
            tables["relative_performance"].append({"period_id": self.period_id,
                "strategy_id": "c0_three_module_fixed", "benchmark_id": family + "_net", "scenario_id": "main",
                **relative_metrics(strategy, benchmark, annualization)})
        overall = {row["account_id"]: row for row in performance if row["period_id"] == self.period_id}
        tables["attribution"] = []
        for family in ("c0_three_module_fixed", "eligible_passive", "common_support_passive"):
            for transition, left, right in (("ideal_to_executed_gross", "ideal_target_gross", "executed_proxy_gross"),
                                             ("executed_gross_to_net", "executed_proxy_gross", "executed_proxy_net"),
                                             ("ideal_to_net_total", "ideal_target_gross", "executed_proxy_net")):
                left_row = overall[family + "__" + left]
                right_row = overall[family + "__" + right]
                tables["attribution"].append({"period_id": self.period_id, "family_id": family,
                    "transition_id": transition, "scenario_id": "main",
                    "left_cumulative_return": left_row["cumulative_return"],
                    "right_cumulative_return": right_row["cumulative_return"],
                    "cumulative_return_gap": right_row["cumulative_return"] - left_row["cumulative_return"],
                    "sequence_note": "ordered_decomposition_not_unique_causal_attribution"})
        tables["execution_summary"] = [self.execution_summary()]
        tables["exposure_daily"] = self.exposure_daily()
        tables["research_trial"] = self.trial_rows()
        return tables

    def execution_summary(self):
        root = self.step5_run / "l5"
        events = fetch_dicts(self.db, "SELECT * FROM read_parquet(?) WHERE event_date BETWEEN ? AND ?",
            [str(root / "order_event/part-00000.parquet"), self.start, self.end])
        fills = fetch_dicts(self.db, "SELECT * FROM read_parquet(?) WHERE execution_date BETWEEN ? AND ?",
            [str(root / "fill/part-00000.parquet"), self.start, self.end])
        costs = fetch_dicts(self.db, "SELECT * FROM read_parquet(?) WHERE trade_date BETWEEN ? AND ?",
            [str(root / "cost_daily/part-00000.parquet"), self.start, self.end])
        requested = sum(float(row["requested_notional_cny"]) for row in events if row["event_type"] == "submitted")
        filled = sum(float(row["notional_cny"]) for row in fills)
        reasons = {}
        for row in events:
            if row["event_type"] == "rejected":
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        return {"period_id": self.period_id, "strategy_id": "c0_three_module_fixed", "scenario_id": "main",
            "order_count": len({row["order_id"] for row in events}), "fill_count": len(fills),
            "partial_fill_count": sum(row["event_type"] == "partially_filled" for row in events),
            "rejected_order_count": sum(row["event_type"] == "rejected" for row in events),
            "requested_notional_cny": requested, "filled_notional_cny": filled,
            "amount_fill_rate": filled / requested if requested else None,
            "commission_cny": sum(float(row["commission_cny"]) for row in fills),
            "transfer_cny": sum(float(row["transfer_cny"]) for row in fills),
            "tax_cny": sum(float(row["tax_cny"]) for row in fills),
            "impact_cny": sum(float(row["impact_cny"]) for row in fills),
            "total_cost_cny": sum(float(row["total_cost_cny"]) for row in fills),
            "mean_daily_one_way_turnover": sum(float(row["one_way_turnover"]) for row in costs) / len(costs) if costs else None,
            "rejection_reasons_json": json.dumps(reasons, sort_keys=True),
            "capacity_unit_status": "not_validated"}

    def exposure_daily(self):
        lots = self.step5_run / "l5/position_lot/part-00000.parquet"
        nav = self.step5_run / "l5/nav_daily/part-00000.parquet"
        query = f"""WITH ref AS (
            SELECT u.trade_date,avg(f.log_circ_mv ORDER BY f.ts_code) mean,
                   stddev_pop(f.log_circ_mv ORDER BY f.ts_code) std,
                   quantile_cont(f.log_circ_mv,1.0/3) q1,quantile_cont(f.log_circ_mv,2.0/3) q2
            FROM {self.accounts.universe} u JOIN {self.accounts.feature} f USING(trade_date,ts_code)
            WHERE u.other_eligible GROUP BY u.trade_date),
        pos AS (
            SELECT l.valuation_date trade_date,l.ts_code,sum(l.reference_value_cny ORDER BY l.lot_id) position_value,
                   max(f.industry_l1) industry_l1,max(f.log_circ_mv) log_circ_mv
            FROM read_parquet({quote_string(str(lots))}) l
            LEFT JOIN {self.accounts.feature} f ON f.trade_date=l.valuation_date AND f.ts_code=l.ts_code
            WHERE l.valuation_date BETWEEN {quote_string(str(self.start))} AND {quote_string(str(self.end))}
            GROUP BY l.valuation_date,l.ts_code)
        SELECT p.trade_date,'c0_three_module_fixed' strategy_id,
               sum(p.position_value*p.position_value/(n.reference_nav_cny*n.reference_nav_cny) ORDER BY p.ts_code) hhi,
               1.0/nullif(sum(p.position_value*p.position_value/(n.reference_nav_cny*n.reference_nav_cny) ORDER BY p.ts_code),0) effective_position_count,
               max(p.position_value/n.reference_nav_cny) top1_weight,
               sum(CASE WHEN p.industry_l1 IS NULL OR p.industry_l1='__UNKNOWN__' THEN p.position_value ELSE 0 END ORDER BY p.ts_code)/n.reference_nav_cny unknown_industry_weight,
               sum(CASE WHEN r.std>0 THEN p.position_value*(p.log_circ_mv-r.mean)/r.std ELSE 0 END ORDER BY p.ts_code)/n.reference_nav_cny size_z,
               sum(CASE WHEN p.log_circ_mv<=r.q1 THEN p.position_value ELSE 0 END ORDER BY p.ts_code)/n.reference_nav_cny small_weight,
               sum(CASE WHEN p.log_circ_mv>r.q1 AND p.log_circ_mv<=r.q2 THEN p.position_value ELSE 0 END ORDER BY p.ts_code)/n.reference_nav_cny mid_weight,
               sum(CASE WHEN p.log_circ_mv>r.q2 THEN p.position_value ELSE 0 END ORDER BY p.ts_code)/n.reference_nav_cny large_weight,
               max(n.total_cash/n.reference_nav_cny) cash_weight,max(n.stale_weight) stale_weight,
               max(n.non_tradable_weight) non_tradable_weight
        FROM pos p JOIN read_parquet({quote_string(str(nav))}) n ON n.trade_date=p.trade_date
        JOIN ref r ON r.trade_date=p.trade_date GROUP BY p.trade_date,n.reference_nav_cny ORDER BY p.trade_date"""
        return fetch_dicts(self.db, query)

    def trial_rows(self):
        protocol = self.config["research_protocol"]
        rows = []
        run_ids = set(self.config["step7"]["candidate_ids_to_run"] + self.config["step7"]["diagnostic_ids_to_run"])
        for candidate in protocol["eligible_candidate_ids"] + protocol["diagnostic_candidate_ids"]:
            completed = candidate in run_ids
            rows.append({"research_activity_id": "stage3_r1_v2", "candidate_id": candidate,
                "role": "eligible" if candidate in protocol["eligible_candidate_ids"] else "diagnostic",
                "status": "completed" if completed else "not_run_missing_independent_execution_account",
                "period_id": self.period_id if completed else None, "parent_step4_run": self.step4_run.name if completed else None,
                "parent_step5_run": self.step5_run.name if completed else None,
                "seen_scope": "design_only" if completed else "none",
                "validation_seen": False, "confirmation_seen": False, "oos_accessed": False,
                "economic_promoted": False})
        return rows
