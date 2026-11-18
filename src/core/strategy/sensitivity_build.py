"""Step 12 design-only R1 sensitivity accounts and diagnostics."""
import copy
from pathlib import Path

from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import parse_ymd
from core.strategy.execution_build import Step5Builder
from core.strategy.inputs import quote_string
from core.strategy.r1_engine import R1Engine
from core.strategy.research import extended_performance, segment_definitions
from core.strategy.sensitivity import relative_ols


class Step12Builder:
    def __init__(self, inputs, step4_run, step5_run, step7_run, config):
        self.inputs = inputs
        self.db = inputs.db
        self.config = config
        self.step4_run, self.step5_run, self.step7_run = Path(step4_run), Path(step5_run), Path(step7_run)
        self.step5 = Step5Builder(inputs, step4_run, config)
        self.start = parse_ymd(config["research_protocol"]["design_period"][0])
        self.end = parse_ymd(config["research_protocol"]["design_period"][1])
        self.initial_capital = float(config["objective"]["initial_capital_cny"])

    def source_nav(self, account_id):
        return fetch_dicts(self.db, """SELECT trade_date,total_cash,reference_position_value_cny,
            stress_position_value_cny,reference_nav_cny,stress_nav_cny FROM read_parquet(?)
            WHERE account_id=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date""",
            [str(self.step7_run / "l5/account_nav_daily/part-00000.parquet"), account_id, self.start, self.end])

    def orders(self):
        cursor = self.db.execute(f"SELECT * FROM read_parquet({quote_string(str(self.step5.order_path))}) ORDER BY planned_execution_date,order_id")
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def execute(self, scenario_id, orders, config):
        engine = R1Engine(config, self.step5.calendar.dates, self.step5.facts_provider,
                          orders, nav_only=True)
        return engine.run(end_date=self.end)["nav_daily"]

    def build(self):
        orders = self.orders()
        accounts = {"main_net": self.source_nav("c0_three_module_fixed__executed_proxy_net")}
        stress = copy.deepcopy(self.config)
        stress["costs"]["main"] = copy.deepcopy(self.config["costs"]["stress"])
        accounts["stress_cost"] = self.execute("stress_cost", orders, stress)
        volatility_orders = [dict(order, return_volatility_20=float(order["return_volatility_20"]) *
            float(self.config["step12"]["volatility_multiplier"])) for order in orders]
        accounts["volatility_2x"] = self.execute("volatility_2x", volatility_orders, self.config)
        for scenario_id, participation in self.config["step12"]["capacity_participation"].items():
            variant = copy.deepcopy(self.config)
            variant["execution"]["daily_amount_participation"] = str(participation)
            accounts[scenario_id] = self.execute(scenario_id, orders, variant)
        nav_rows = []
        for scenario_id, rows in accounts.items():
            for row in rows:
                nav_rows.append({"trade_date": row["trade_date"], "sensitivity_id": scenario_id, "scenario_id": "main",
                    "total_cash": row["total_cash"], "reference_position_value_cny": row["reference_position_value_cny"],
                    "stress_position_value_cny": row["stress_position_value_cny"],
                    "reference_nav_cny": row["reference_nav_cny"], "stress_nav_cny": row["stress_nav_cny"],
                    "accounting_mode": "adjusted_unit_proxy"})
        performance = []
        annualization = int(self.config["research_protocol"]["annualization_days"])
        segments = segment_definitions(self.start, self.end)
        for scenario_id, rows in sorted(accounts.items()):
            for period_id, start, end in segments:
                selected = [row for row in rows if start <= row["trade_date"] <= end]
                prior = [row for row in rows if row["trade_date"] < start]
                baseline = float(prior[-1]["reference_nav_cny"]) if prior else self.initial_capital
                performance.append({"period_id": period_id, "sensitivity_id": scenario_id, "scenario_id": "main",
                    "baseline_nav_cny": baseline, **extended_performance(selected, baseline, annualization)})
        main_rows = accounts["main_net"]
        sample = [row for row in performance if row["sensitivity_id"] == "main_net"]
        ols_rows = []
        for benchmark_id, account_id in (("eligible_passive_net", "eligible_passive__executed_proxy_net"),
                                         ("common_support_passive_net", "common_support_passive__executed_proxy_net")):
            benchmark = self.source_nav(account_id)
            ols_rows.append({"period_id": "design", "sensitivity_id": "main_net", "benchmark_id": benchmark_id,
                **relative_ols(main_rows, benchmark, self.initial_capital, annualization)})
        universe = []
        for account_id in ("eligible_passive__executed_proxy_net", "common_support_passive__executed_proxy_net"):
            source = next(row for row in fetch_dicts(self.db, "SELECT * FROM read_parquet(?) WHERE period_id='design'",
                [str(self.step7_run / "l5/performance/part-00000.parquet")]) if row["account_id"] == account_id)
            universe.append({"period_id": "design", "universe_account_id": account_id,
                "cumulative_return": source["cumulative_return"], "annualized_return": source["annualized_return"],
                "max_drawdown": source["max_drawdown"], "scope": "benchmark_pool_only_not_new_c0_target"})
        registry = [{"sensitivity_id": scenario_id, "status": "completed", "seen_scope": "design_only",
                     "economic_promoted": False} for scenario_id in accounts]
        registry += [
            {"sensitivity_id": "sample_splits", "status": "completed", "seen_scope": "design_only", "economic_promoted": False},
            {"sensitivity_id": "relative_ols_nav", "status": "completed", "seen_scope": "design_only", "economic_promoted": False},
            {"sensitivity_id": "universe_benchmark_pools", "status": "completed_benchmark_only", "seen_scope": "design_only", "economic_promoted": False},
            {"sensitivity_id": "c0_other_eligible_target", "status": "not_run_requires_new_target_trial", "seen_scope": "none", "economic_promoted": False},
            {"sensitivity_id": "c0_base_pool_target", "status": "not_run_requires_new_target_trial", "seen_scope": "none", "economic_promoted": False}]
        return {"sensitivity_account_nav": nav_rows, "sensitivity_performance": performance,
                "sample_split_performance": sample, "relative_ols": ols_rows,
                "universe_sensitivity": universe, "sensitivity_registry": registry}
