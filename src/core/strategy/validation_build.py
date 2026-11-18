"""Step 8 frozen-candidate validation on 2022-2024 only."""
import json
from pathlib import Path

from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import file_sha256
from core.strategy.promotion import boundary_check, promotion_gate
from core.strategy.research_build import Step7Builder


class Step8Builder:
    def __init__(self, inputs, step3_run, step4_run, step5_run, step7_run, config, project_root):
        self.inputs = inputs
        self.config = config
        self.step7_run = Path(step7_run)
        self.preregistration_path = Path(project_root) / config["step8"]["preregistration"]
        self.preregistration = json.loads(self.preregistration_path.read_text(encoding="utf-8"))
        if self.preregistration["candidate_id"] != config["step8"]["candidate_id"]:
            raise ValueError("Step 8 candidate differs from pre-registration")
        if self.preregistration["design_run_id"] != self.step7_run.name:
            raise ValueError("Step 8 design parent differs from pre-registration")
        self.research = Step7Builder(inputs, step3_run, step4_run, step5_run, config,
            period_id="validation", period_range=config["research_protocol"]["validation_period"],
            account_start=config["research_protocol"]["design_period"][0])

    def build(self):
        tables = self.research.build()
        tables["research_trial"] = self.validation_trials()
        tables["candidate_freeze"] = [{
            "strategy_id": self.preregistration["candidate_id"], "version": self.preregistration["version"],
            "freeze_id": self.preregistration["freeze_id"], "design_run_id": self.preregistration["design_run_id"],
            "preregistration_sha256": file_sha256(self.preregistration_path),
            "config_sha256": self.preregistration["config_sha256"],
            "code_bundle_sha256": self.preregistration["code_bundle_sha256"],
            "validation_start": self.preregistration["validation_period"][0],
            "validation_end": self.preregistration["validation_period"][1],
            "frozen_before_validation_evaluation": True}]
        design_performance = fetch_dicts(self.research.db, "SELECT * FROM read_parquet(?)",
            [str(self.step7_run / "l5/performance/part-00000.parquet")])
        design_relative = fetch_dicts(self.research.db, "SELECT * FROM read_parquet(?)",
            [str(self.step7_run / "l5/relative_performance/part-00000.parquet")])
        design_exposure = fetch_dicts(self.research.db, "SELECT * FROM read_parquet(?)",
            [str(self.step7_run / "l5/exposure_daily/part-00000.parquet")])
        thresholds = self.config["step8"]["economic_boundaries"]
        design = boundary_check(design_performance, design_relative, design_exposure, "design", thresholds)
        validation = boundary_check(tables["performance"], tables["relative_performance"],
                                    tables["exposure_daily"], "validation", thresholds)
        tables["promotion_gate"] = [promotion_gate(design, validation)]
        return tables

    def validation_trials(self):
        protocol = self.config["research_protocol"]
        frozen = self.config["step8"]["candidate_id"]
        rows = []
        for candidate in protocol["eligible_candidate_ids"] + protocol["diagnostic_candidate_ids"]:
            completed = candidate == frozen
            rows.append({"research_activity_id": "stage3_r1_v2", "candidate_id": candidate,
                "role": "eligible" if candidate in protocol["eligible_candidate_ids"] else "diagnostic",
                "status": "completed" if completed else "not_run_not_frozen_for_validation",
                "period_id": "validation" if completed else None,
                "parent_step4_run": self.research.step4_run.name if completed else None,
                "parent_step5_run": self.research.step5_run.name if completed else None,
                "seen_scope": "design_and_validation" if completed else "none",
                "validation_seen": completed, "confirmation_seen": False, "oos_accessed": False,
                "economic_promoted": False})
        return rows
