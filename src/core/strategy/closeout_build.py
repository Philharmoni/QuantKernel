"""Build and atomically publish the Step 14 R1 evidence closeout."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.quality.verification import verification_metadata, verification_mode
from core.strategy.closeout import REQUIRED_STEPS, closeout_report, closeout_status, step_evidence_errors
from core.strategy.contract import (architecture_violations, canonical_hash, file_sha256,
                                    load_stage3_contract, verify_protected_hashes)


def independent_repeat_complete(runs):
    return all(item.get("verification", "audit") == "audit" and
               item.get("repeat_status", "verified_existing") in {"executed", "verified_existing"}
               for item in runs.values())


class Step14Builder:
    def __init__(self, config, project_root=PROJECT_ROOT, verification="standard"):
        self.verification = verification_mode(verification)
        self.config = config
        self.root = Path(project_root)
        self.research_root = self.root / config["delivery"]["root"]
        if tuple(config["step14"]["required_steps"]) != REQUIRED_STEPS:
            raise ValueError("Step 14 required evidence steps differ from the frozen closeout scope")

    def _json(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def build(self):
        if not self.config["delivery"].get("can_enter_step14"):
            raise PermissionError("Step 13 has not opened the Step 14 closeout")
        errors = {}
        runs = {}
        for step in REQUIRED_STEPS:
            manifest_path = self.root / self.config["delivery"][f"step{step}_manifest"]
            verification_path = self.root / self.config["delivery"][f"step{step}_verification"]
            manifest = self._json(manifest_path)
            verification = self._json(verification_path)
            active_run = self.config["delivery"][f"active_step{step}_run"]
            step_errors = step_evidence_errors(step, Path(active_run).name, manifest, verification)
            registered = manifest.get("verification", {})
            verification_hash = file_sha256(verification_path)
            if registered.get("sha256") != verification_hash:
                step_errors.append("verification_hash")
            expected_relative = verification_path.relative_to(manifest_path.parent).as_posix()
            if registered.get("path") != expected_relative:
                step_errors.append("verification_path")
            if verification.get("observations", {}).get("research_labels_read") is True:
                step_errors.append("research_labels_read")
            if step_errors:
                errors[str(step)] = sorted(set(step_errors))
            runs[str(step)] = {"run_id": active_run,
                "manifest_path": manifest_path.relative_to(self.root).as_posix(),
                "manifest_sha256": file_sha256(manifest_path),
                "verification_path": verification_path.relative_to(self.root).as_posix(),
                "verification_sha256": verification_hash, "passed": verification.get("passed") is True,
                "verification": verification.get("verification", "audit"),
                "repeat_status": verification.get("repeat_status", "verified_existing")}
        protected = verify_protected_hashes(self.root)
        if not protected["passed"]:
            errors["protected_scope"] = protected["differences"]
        architecture = architecture_violations(self.root / "src/core")
        if architecture:
            errors["architecture"] = architecture
        admission_path = self.root / self.config["delivery"]["input_bundle_admission"]
        admission = self._json(admission_path)
        if (admission.get("passed") is not True or not admission.get("all_dates_bounded")
                or admission.get("oos_performance_generated") is not False):
            errors["input_admission"] = ["not_passed_or_unbounded"]
        decision_path = self.research_root / "runs" / self.config["delivery"]["active_step9_run"] / "evidence/promotion_decision.json"
        r2_path = self.research_root / "runs" / self.config["delivery"]["active_step10_run"] / "evidence/r2_quality_gate.json"
        decision, r2_gate = self._json(decision_path), self._json(r2_path)
        if (decision.get("decision") != "no_champion_no_promotion" or decision.get("economic_promoted") is not False
                or decision.get("confirmation_seen") is not False or decision.get("oos_accessed") is not False):
            errors["promotion_decision"] = ["negative_decision_changed"]
        if (r2_gate.get("passed") is not False or r2_gate.get("r2_backtest_permitted") is not False
                or r2_gate.get("can_enter_step11") is not False or len(r2_gate.get("blockers", [])) != 8):
            errors["r2_gate"] = ["negative_gate_or_blockers_changed"]
        for key in ("economic_promoted", "real_share_account_ready", "live_ready", "oos_accessed"):
            if self.config["delivery"][key] is not False:
                errors.setdefault("delivery", []).append(key)
        if self.config["delivery"].get("r2_backtest_permitted") is not False \
                or self.config["delivery"].get("can_enter_step11") is not False:
            errors.setdefault("delivery", []).append("r2_or_step11_opened")
        if errors:
            raise ValueError("Step 14 closeout evidence failed: " + json.dumps(errors, ensure_ascii=False, default=str))
        metrics = self._json(self.research_root / "runs" / self.config["delivery"]["active_step9_run"] /
                             "evidence/report_metrics.json")
        account = "c0_three_module_fixed__executed_proxy_net"
        step4 = self._json(self.root / self.config["delivery"]["step4_verification"])["observations"]
        step13 = self._json(self.root / self.config["delivery"]["step13_verification"])["observations"]
        repeat_complete = independent_repeat_complete(runs)
        result = {"step": 14, "closeout_status": "r1_complete_no_champion",
            **closeout_status(decision, r2_gate, True), "accounting_mode": "adjusted_unit_proxy",
            "fill_model": "proxy_not_observed", "design_seen": True, "validation_seen": True,
            "new_backtest": False, "data_rows_read": 0,
            "key_results": {period: metrics[period][account] for period in ("design", "validation")},
            "protected_files": protected, "architecture_violations": architecture,
            "portfolio": {"solved_days": step4["solved_days"], "infeasible_days": step4["infeasible_days"]},
            "robustness": {"truncation_prefix_mismatch": step13["truncation_prefix_mismatch"],
                           "future_prefix_mismatch": step13["future_prefix_mismatch"]},
            "r2_blockers": r2_gate["blockers"], "known_boundaries": self.config["boundaries"],
            "independent_repeat_complete": repeat_complete, **verification_metadata(self.verification),
            "audit_complete": self.verification == "audit" and repeat_complete}
        gate_specs = (
            ("G01", "upstream_and_frozen_protection", "contract,input_admission,protected_hashes"),
            ("G02", "permission_and_oos", "step7,step8,step9,step10,step12,step13"),
            ("G03", "architecture_isolation", "static_source_scan,runtime_read_logs"),
            ("G04", "schema_and_lineage", "step3-step10,step12-step13_manifests"),
            ("G05", "pit_timeline", "step3,step6,step13"),
            ("G06", "golden_exceptions", "step1_fixture,step4-step6_regression"),
            ("G07", "execution_accounting", "step5,step7,step8"),
            ("G08", "portfolio_risk", "step4_fixed_degradation"),
            ("G09", "truncation", "step3,step6,step13"),
            ("G10", "independent_repeat", "all_required_step_verifications"),
            ("G11", "evaluation_attribution_pressure", "step7,step8,step12,step13"),
            ("G12", "documentation_audit", "step9_reports,step14_closeout"),
        )
        matrix = [{"gate_id": gate_id, "check": check, "passed": True, "evidence": evidence,
                   "method": "immutable_evidence_reconciliation_not_new_backtest"}
                  for gate_id, check, evidence in gate_specs]
        for row in matrix:
            if row["gate_id"] == "G10":
                row["passed"] = True if repeat_complete else None
                row["evidence"] = {"steps": runs, "status": "complete" if repeat_complete else "not_run"}
        input_manifest_path = self.root / self.config["delivery"]["input_bundle_manifest"]
        evidence = {"method": "manifest_and_verification_identity_reconciliation",
            "artifact_payload_rehashed": False, "runs": runs,
            "input_bundle": self.config["delivery"]["active_input_bundle"],
            "input_manifest_sha256": file_sha256(input_manifest_path),
            "input_admission_sha256": file_sha256(admission_path),
            "promotion_decision_sha256": file_sha256(decision_path),
            "r2_quality_gate_sha256": file_sha256(r2_path),
            "plan_sha256": file_sha256(self.root / self.config["plan"]),
            "registry_sha256": file_sha256(self.root / "configs/strategy_registry.yaml"),
            "preregistration_sha256": file_sha256(self.root / self.config["step8"]["preregistration"]),
            "design_seen": True, "validation_seen": True, "confirmation_seen": False, "oos_accessed": False,
            **verification_metadata(self.verification)}
        return {"closeout": result, "evidence": evidence, "gate_matrix": matrix,
                "report": closeout_report(result, evidence, matrix)}


def default_step14_run_id(project_root=PROJECT_ROOT, use_active=True, verification="standard"):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step14_run")
    if use_active and active:
        return active
    identity = {"step": 14, "step14": config["step14"],
        "layout": config["protected_scope"].get("scope", "before_stage3_changes"),
        "verification": verification_mode(verification),
        "evidence": {str(step): {
            "manifest": file_sha256(project_root / config["delivery"][f"step{step}_manifest"]),
            "verification": file_sha256(project_root / config["delivery"][f"step{step}_verification"])}
            for step in REQUIRED_STEPS},
        "code": {name: file_sha256(Path(__file__).with_name(name))
                 for name in ("closeout.py", "closeout_build.py")}}
    return config["delivery"]["active_run_group"] + "/step14"


def publish_step14(builder, output_root, run_id):
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 14 output: {output_root}")
    result = builder.build()
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 14 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        files = {}
        for relative, value in (("evidence/r1_closeout.json", result["closeout"]),
                                ("evidence/r1_evidence_index.json", result["evidence"]),
                                ("evidence/r1_gate_matrix.json", result["gate_matrix"])):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
                              encoding="utf-8")
            files[relative] = file_sha256(target)
        target = staging / "reports/r1_closeout_report.md"
        target.parent.mkdir(parents=True)
        target.write_text(result["report"], encoding="utf-8")
        files[str(target.relative_to(staging))] = file_sha256(target)
        closeout = result["closeout"]
        manifest = {"step": 14, "run_id": run_id,
            "run_hash": "step14_" + canonical_hash(files)[:16], "status": "complete",
            "closeout_status": closeout["closeout_status"], "evidence_steps": list(REQUIRED_STEPS),
            "gate_count": len(result["gate_matrix"]), "new_backtest": False, "data_rows_read": 0,
            "research_labels_read": False, "input_read_log": [], "files": files,
            **{key: closeout[key] for key in ("r1_research_complete", "engineering_passed", "economic_promoted",
                "r2_quality_gate_passed", "r2_backtest_permitted", "can_enter_step11",
                "real_share_account_ready", "live_ready", "confirmation_seen", "oos_accessed")}}
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return manifest
    except Exception:
        shutil.rmtree(staging)
        raise
