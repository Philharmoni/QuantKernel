"""Step 13 design-only R1 truncation, PIT and stale-position pressure checks."""
from datetime import timedelta
import json
from pathlib import Path

from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import file_sha256, parse_ymd
from core.strategy.execution_build import Step5Builder
from core.strategy.r1_engine import R1Engine
from core.strategy.robustness import PITFacts, prefix_comparison, pressure_factor, stress_position_value


NAV_FIELDS = ("total_cash", "reference_position_value_cny", "stress_position_value_cny",
              "reference_nav_cny", "stress_nav_cny")


class Step13Builder:
    def __init__(self, inputs, step4_run, step5_run, step7_run, config):
        self.inputs = inputs
        self.db = inputs.db
        self.config = config
        self.step5_run, self.step7_run = Path(step5_run), Path(step7_run)
        self.step5 = Step5Builder(inputs, step4_run, config)
        self.step5_manifest = json.loads((self.step5_run / "manifest.json").read_text(encoding="utf-8"))
        self.step7_manifest = json.loads((self.step7_run / "manifest.json").read_text(encoding="utf-8"))
        for run_root, manifest, tables in ((self.step5_run, self.step5_manifest, ("nav_daily", "position_lot")),
                                           (self.step7_run, self.step7_manifest, ("account_nav_daily",))):
            for table in tables:
                entry = manifest["tables"][table]
                if file_sha256(run_root / entry["path"]) != entry["sha256"]:
                    raise ValueError(f"Step 13 source hash mismatch: {table}")
        self.step5_nav = self.step5_run / self.step5_manifest["tables"]["nav_daily"]["path"]
        self.step5_lots = self.step5_run / self.step5_manifest["tables"]["position_lot"]["path"]
        self.step7_nav = self.step7_run / self.step7_manifest["tables"]["account_nav_daily"]["path"]
        self.start = parse_ymd(config["research_protocol"]["design_period"][0])
        self.end = parse_ymd(config["research_protocol"]["design_period"][1])
        self.cutoff = parse_ymd(config["step13"]["truncation_date"])
        if not self.start <= self.cutoff < self.end:
            raise ValueError("Step 13 truncation date must be inside the design period")
        self.multiplier = float(config["step13"]["future_quote_multiplier"])
        if self.multiplier <= 0 or self.multiplier == 1:
            raise ValueError("Step 13 future quote multiplier must be positive and non-neutral")

    @staticmethod
    def _nav_rows(rows):
        return [{"trade_date": row["trade_date"], **{field: row[field] for field in NAV_FIELDS}} for row in rows]

    def source_nav(self):
        return self._nav_rows(fetch_dicts(self.db, """SELECT trade_date,total_cash,reference_position_value_cny,
            stress_position_value_cny,reference_nav_cny,stress_nav_cny FROM read_parquet(?)
            WHERE account_id='c0_three_module_fixed__executed_proxy_net'
            AND trade_date BETWEEN ? AND ? ORDER BY trade_date""", [str(self.step7_nav), self.start, self.end]))

    def orders(self):
        cursor = self.db.execute("SELECT * FROM read_parquet(?) ORDER BY planned_execution_date,order_id",
                                 [str(self.step5.order_path)])
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def future_facts(self, day, codes):
        rows = self.step5.facts_provider(day, codes)
        if day > self.cutoff:
            for row in rows.values():
                for field in ("open", "close"):
                    if row.get(field) is not None:
                        row[field] = float(row[field]) * self.multiplier
        return rows

    def execute(self, orders, end_date, facts_provider):
        scores = {(row[0], row[1]): float(row[2]) for row in self.db.execute(
            "SELECT signal_date,ts_code,score FROM read_parquet(?) WHERE score IS NOT NULL",
            [str(self.step5.target_path)]).fetchall()}
        engine = R1Engine(self.config, self.step5.calendar.dates, facts_provider, orders, scores, nav_only=True)
        return self._nav_rows(engine.run(end_date=end_date)["nav_daily"])

    def _comparison(self, comparison_id, full, variant):
        result = prefix_comparison(full, variant, self.cutoff)
        full_after = [row for row in full if row["trade_date"] > self.cutoff]
        variant_after = [row for row in variant if row["trade_date"] > self.cutoff]
        changed = sum(left != right for left, right in zip(full_after, variant_after))
        changed += abs(len(full_after) - len(variant_after))
        return {"comparison_id": comparison_id, "cutoff_date": self.cutoff, **result,
                "full_post_cutoff_rows": len(full_after), "variant_post_cutoff_rows": len(variant_after),
                "post_cutoff_changed_rows": changed}

    def _pit_audit(self, probe_code):
        calls = {"cutoff": 0, "future": 0}

        def provider(kind):
            def call(day, codes):
                calls[kind] += 1
                return self.step5.facts_provider(day, codes)
            return call

        allowed_facts = PITFacts(provider("cutoff"), self.cutoff)(self.cutoff, [probe_code])
        future_day = self.cutoff + timedelta(days=1)
        error_type = ""
        try:
            PITFacts(provider("future"), self.cutoff)(future_day, [probe_code])
        except PermissionError as error:
            error_type = type(error).__name__
        return [
            {"check_id": "cutoff_read_allowed", "cutoff_date": self.cutoff, "requested_date": self.cutoff,
             "access_allowed": True, "blocked": False, "provider_calls": calls["cutoff"],
             "returned_fact_count": len(allowed_facts), "error_type": ""},
            {"check_id": "future_read_blocked", "cutoff_date": self.cutoff, "requested_date": future_day,
             "access_allowed": False, "blocked": error_type == "PermissionError",
             "provider_calls": calls["future"], "returned_fact_count": 0, "error_type": error_type},
        ]

    def _actual_stale_pressure(self):
        return fetch_dicts(self.db, """WITH lot_daily AS (
            SELECT valuation_date AS trade_date,sum(reference_value_cny) AS reference_position_value_cny,
                sum(stress_value_cny) AS stress_position_value_cny,
                sum(CASE WHEN stale_trade_days>0 THEN reference_value_cny ELSE 0 END) AS stale_position_value_cny,
                sum(CASE WHEN non_tradable THEN reference_value_cny ELSE 0 END) AS non_tradable_position_value_cny,
                count(*) FILTER(WHERE stale_trade_days>0) AS stale_position_count,
                count(*) FILTER(WHERE non_tradable) AS non_tradable_position_count,
                max(stale_trade_days) AS max_stale_trade_days
            FROM read_parquet(?) WHERE valuation_date BETWEEN ? AND ? GROUP BY valuation_date)
            SELECT l.*,n.total_cash,n.reference_position_value_cny AS source_reference_position_value_cny,
                n.stress_position_value_cny AS source_stress_position_value_cny,
                n.stress_nav_cny AS source_stress_nav_cny,
                n.total_cash+l.stress_position_value_cny AS rebuilt_stress_nav_cny,
                abs(n.stress_position_value_cny-l.stress_position_value_cny) AS stress_position_abs_error,
                abs(n.stress_nav_cny-n.total_cash-l.stress_position_value_cny) AS stress_nav_abs_error,
                'adjusted_unit_proxy' AS accounting_mode
            FROM lot_daily l JOIN read_parquet(?) n ON n.trade_date=l.trade_date
            ORDER BY l.trade_date""", [str(self.step5_lots), self.start, self.end, str(self.step5_nav)])

    def _synthetic_pressure(self):
        lots = fetch_dicts(self.db, """SELECT valuation_date,ts_code,lot_id,stale_trade_days,non_tradable,
            reference_value_cny FROM read_parquet(?) WHERE valuation_date=? AND reference_value_cny>0
            ORDER BY ts_code,lot_id""", [str(self.step5_lots), self.end])
        if not lots:
            raise ValueError("Step 13 requires terminal open positions for synthetic pressure")
        scenarios = (
            ("synthetic_stale_60_half", int(self.config["step13"]["stale_half_days"]), False),
            ("synthetic_stale_120_zero", int(self.config["step13"]["stale_zero_days"]), False),
            ("synthetic_delisting_zero", int(self.config["step13"]["stale_zero_days"]), True),
        )
        rows = []
        for lot in lots:
            for scenario_id, stale_days, non_tradable in scenarios:
                reference = float(lot["reference_value_cny"])
                rows.append({"valuation_date": lot["valuation_date"], "ts_code": lot["ts_code"],
                    "lot_id": lot["lot_id"], "scenario_id": scenario_id,
                    "source_stale_trade_days": lot["stale_trade_days"], "source_non_tradable": lot["non_tradable"],
                    "scenario_stale_trade_days": stale_days, "scenario_non_tradable": non_tradable,
                    "reference_value_cny": reference, "applied_factor": pressure_factor(stale_days, non_tradable),
                    "stress_value_cny": stress_position_value(reference, stale_days, non_tradable),
                    "cash_release_cny": 0.0, "generated_fill": False, "accounting_mode": "adjusted_unit_proxy"})
        return rows

    def build(self):
        orders = self.orders()
        full = self.source_nav()
        truncated = self.execute(orders, self.cutoff, PITFacts(self.step5.facts_provider, self.cutoff))
        future = self.execute(orders, self.end, self.future_facts)
        nav_rows = []
        for robustness_id, rows in (("full_design", full), ("real_truncation", truncated),
                                    ("future_quote_perturbation", future)):
            nav_rows.extend({"trade_date": row["trade_date"], "robustness_id": robustness_id,
                "scenario_id": "main", **{field: row[field] for field in NAV_FIELDS},
                "accounting_mode": "adjusted_unit_proxy"} for row in rows)
        comparisons = [self._comparison("real_truncation", full, truncated),
                       self._comparison("future_quote_perturbation", full, future)]
        registry = [
            ("design_only_full", "completed_source_account"),
            ("real_truncation", "completed"),
            ("future_quote_perturbation", "completed_synthetic"),
            ("pit_future_read_blocked", "blocked_as_required"),
            ("actual_stale_pressure", "completed_source_account"),
            ("synthetic_stale_60_half", "completed_synthetic_not_realized"),
            ("synthetic_stale_120_zero", "completed_synthetic_not_realized"),
            ("synthetic_delisting_zero", "completed_synthetic_not_realized"),
        ]
        return {"robustness_account_nav": nav_rows, "truncation_comparison": comparisons,
                "pit_access_audit": self._pit_audit(orders[0]["ts_code"]),
                "stale_pressure_daily": self._actual_stale_pressure(),
                "synthetic_position_pressure": self._synthetic_pressure(),
                "robustness_registry": [{"check_id": check_id, "status": status, "seen_scope": "design_only",
                    "economic_promoted": False, "r2_backtest_permitted": False} for check_id, status in registry]}
