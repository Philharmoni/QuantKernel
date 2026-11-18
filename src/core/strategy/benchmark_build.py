"""Build Step 6 strategy/passive ideal, executed gross and executed net accounts."""
import copy
import json
from pathlib import Path

from core.strategy.benchmark import (IdealTargetAccount, market_value_targets, passive_orders,
                                     performance_metrics, relative_metrics)
from core.strategy.contract import parse_ymd
from core.strategy.execution_build import Step5Builder
from core.strategy.inputs import quote_string
from core.strategy.r1_engine import R1Engine


def fetch_dicts(db, query, params=None):
    cursor = db.execute(query, params or [])
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def gross_config(config):
    result = copy.deepcopy(config)
    for field in ("commission_rate", "commission_minimum_cny_per_stock_direction_day",
                  "transfer_fee_rate", "sell_tax_rate", "impact_fixed_rate", "impact_volatility_coefficient"):
        result["costs"]["main"][field] = "0"
    return result


class Step6Builder:
    def __init__(self, inputs, step3_run, step4_run, step5_run, config,
                 period_id="step6_small_window", nav_only=False):
        self.inputs = inputs
        self.db = inputs.db
        self.config = config
        self.period_id = period_id
        self.nav_only = nav_only
        self.step3_run, self.step4_run, self.step5_run = Path(step3_run), Path(step4_run), Path(step5_run)
        self.step5 = Step5Builder(inputs, step4_run, config)
        manifest = json.loads((self.step5_run / "manifest.json").read_text(encoding="utf-8"))
        from core.strategy.contract import file_sha256
        entry = manifest["tables"]["nav_daily"]
        if file_sha256(self.step5_run / entry["path"]) != entry["sha256"]:
            raise ValueError("Step 5 NAV input hash mismatch")
        self.step5_nav = self.step5_run / entry["path"]
        self.universe = f"read_parquet({quote_string(str(self.step3_run / 'l3/strategy_universe/part-00000.parquet'))})"
        self.feature = f"read_parquet({quote_string(str(self.step3_run / 'l3/strategy_feature/part-00000.parquet'))})"
        self.targets = f"read_parquet({quote_string(str(self.step4_run / 'l4/target_portfolio/part-00000.parquet'))})"
        self.daily = f"read_parquet({quote_string(str(self.step4_run / 'l4/portfolio_daily/part-00000.parquet'))})"
        self.start = parse_ymd(config["step6"]["small_window"][0])
        self.end = parse_ymd(config["step6"]["small_window"][1])
        self.initial_capital = float(config["objective"]["initial_capital_cny"])

    def facts(self, day, codes, perturb_after=None):
        rows = self.step5.facts_provider(day, codes)
        if perturb_after is not None and day > perturb_after:
            for row in rows.values():
                for field in ("open", "close"):
                    if row.get(field) is not None:
                        row[field] = float(row[field]) * float(self.config["step6"]["future_quote_multiplier"])
        return rows

    def schedule(self, end_date):
        return fetch_dicts(self.db, f"""SELECT signal_date,planned_execution_date FROM {self.daily}
            WHERE signal_date>=? AND planned_execution_date<=? ORDER BY signal_date""", [self.start, end_date])

    def strategy_targets(self, schedule):
        result = {}
        for row in schedule:
            holdings = fetch_dicts(self.db, f"""SELECT ts_code,target_weight FROM {self.targets}
                WHERE signal_date=? AND target_weight>0 ORDER BY ts_code""", [row["signal_date"]])
            result[row["planned_execution_date"]] = {item["ts_code"]: float(item["target_weight"]) for item in holdings}
        return result

    def passive_inputs(self, schedule, matched):
        targets, infos, audit = {}, {}, []
        benchmark_id = "common_support_passive" if matched else "eligible_passive"
        condition = "u.is_signal_eligible" if matched else "u.other_eligible"
        for row in schedule:
            signal_date, execution_date = row["signal_date"], row["planned_execution_date"]
            reference = fetch_dicts(self.db, f"""SELECT f.ts_code,f.circ_mv,f.adv20_cny,f.return_volatility_20
                FROM {self.universe} u JOIN {self.feature} f USING(trade_date,ts_code)
                WHERE u.trade_date=? AND {condition} ORDER BY f.ts_code""", [signal_date])
            weights = market_value_targets(reference, float(self.config["objective"]["stock_target_weight"]))
            key = (signal_date, execution_date)
            targets[key] = weights
            infos[key] = {item["ts_code"]: item for item in reference}
            for item in reference:
                audit.append({"signal_date": signal_date, "planned_execution_date": execution_date,
                    "ts_code": item["ts_code"], "benchmark_id": benchmark_id,
                    "target_weight": weights[item["ts_code"]], "circ_mv": item["circ_mv"]})
        return targets, infos, audit

    @staticmethod
    def _account_rows(rows, account_id, family_id, role, model):
        return [{"trade_date": row["trade_date"], "account_id": account_id, "scenario_id": "main",
            "family_id": family_id, "account_role": role, "return_model": model,
            "total_cash": row["total_cash"], "reference_position_value_cny": row["reference_position_value_cny"],
            "stress_position_value_cny": row["stress_position_value_cny"],
            "reference_nav_cny": row["reference_nav_cny"], "stress_nav_cny": row["stress_nav_cny"],
            "accounting_mode": "adjusted_unit_proxy"} for row in rows]

    def _executed(self, orders, end_date, config, perturb_after, risk_templates=None):
        engine = R1Engine(config, self.step5.calendar.dates,
            lambda day, codes: self.facts(day, codes, perturb_after), orders,
            risk_templates=risk_templates, nav_only=self.nav_only)
        return engine.run(end_date=end_date)["nav_daily"]

    def build(self, end_date=None, perturb_after=None):
        end_date = min(self.end, end_date or self.end)
        schedule = self.schedule(end_date)
        if not schedule:
            raise ValueError("Step 6 small window has no rebalance schedule")
        nav_rows, benchmark_audit = [], []
        raw_accounts, families = {}, {}
        strategy_targets = self.strategy_targets(schedule)
        ideal = IdealTargetAccount(self.initial_capital, self.step5.calendar.dates,
            lambda day, codes: self.facts(day, codes, perturb_after), strategy_targets).run(end_date)
        raw_accounts["c0_three_module_fixed__ideal_target_gross"] = ideal
        families["c0_three_module_fixed"] = {"ideal": "c0_three_module_fixed__ideal_target_gross"}
        cursor = self.db.execute(f"SELECT * FROM read_parquet({quote_string(str(self.step5.order_path))}) ORDER BY planned_execution_date,order_id")
        names = [item[0] for item in cursor.description]
        strategy_orders = [dict(zip(names, row)) for row in cursor.fetchall()]
        gross = self._executed(strategy_orders, end_date, gross_config(self.config), perturb_after)
        gross_id = "c0_three_module_fixed__executed_proxy_gross"
        raw_accounts[gross_id] = gross
        families["c0_three_module_fixed"]["gross"] = gross_id
        net_id = "c0_three_module_fixed__executed_proxy_net"
        net = fetch_dicts(self.db, "SELECT * FROM read_parquet(?) WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                          [str(self.step5_nav), min(strategy_targets), end_date])
        raw_accounts[net_id] = net
        families["c0_three_module_fixed"]["net"] = net_id
        net_accounts = {net_id: net}
        for matched in (False, True):
            family = "common_support_passive" if matched else "eligible_passive"
            targets, infos, audit = self.passive_inputs(schedule, matched)
            benchmark_audit.extend(audit)
            orders = passive_orders(targets, infos, self.initial_capital, family)
            ideal_id = family + "__ideal_target_gross"
            raw_accounts[ideal_id] = IdealTargetAccount(self.initial_capital, self.step5.calendar.dates,
                lambda day, codes: self.facts(day, codes, perturb_after),
                {execution: weights for (_, execution), weights in targets.items()}).run(end_date)
            templates = {}
            for key in sorted(infos):
                templates.update(infos[key])
            gross_id = family + "__executed_proxy_gross"
            raw_accounts[gross_id] = self._executed(orders, end_date, gross_config(self.config), perturb_after, templates)
            net_id = family + "__executed_proxy_net"
            raw_accounts[net_id] = self._executed(orders, end_date, self.config, perturb_after, templates)
            net_accounts[net_id] = raw_accounts[net_id]
            families[family] = {"ideal": ideal_id, "gross": gross_id, "net": net_id}
        for account_id, rows in raw_accounts.items():
            family = next(name for name in families if account_id.startswith(name + "__"))
            model = account_id.split("__", 1)[1]
            role = "strategy" if family == "c0_three_module_fixed" else "benchmark"
            nav_rows.extend(self._account_rows(rows, account_id, family, role, model))
        performance = [{"period_id": self.period_id, "account_id": account_id, "scenario_id": "main",
            **performance_metrics(rows, self.initial_capital, int(self.config["research_protocol"]["annualization_days"]))}
            for account_id, rows in sorted(raw_accounts.items())]
        strategy_net = raw_accounts[families["c0_three_module_fixed"]["net"]]
        relative = []
        for family in ("eligible_passive", "common_support_passive"):
            benchmark_net = raw_accounts[families[family]["net"]]
            relative.append({"period_id": self.period_id, "strategy_id": "c0_three_module_fixed",
                "benchmark_id": family + "_net", "scenario_id": "main",
                **relative_metrics(strategy_net, benchmark_net, int(self.config["research_protocol"]["annualization_days"]))})
        attribution = []
        for family, accounts in sorted(families.items()):
            values = {role: performance_metrics(raw_accounts[account_id], self.initial_capital)["cumulative_return"]
                      for role, account_id in accounts.items()}
            for transition, left, right in (("ideal_to_executed_gross", "ideal", "gross"),
                                             ("executed_gross_to_net", "gross", "net"),
                                             ("ideal_to_net_total", "ideal", "net")):
                attribution.append({"period_id": self.period_id, "family_id": family,
                    "transition_id": transition, "scenario_id": "main",
                    "left_cumulative_return": values[left], "right_cumulative_return": values[right],
                    "cumulative_return_gap": values[right] - values[left],
                    "sequence_note": "ordered_decomposition_not_unique_causal_attribution"})
        return {"benchmark_target": benchmark_audit, "account_nav_daily": nav_rows,
                "performance": performance, "relative_performance": relative, "attribution": attribution}
