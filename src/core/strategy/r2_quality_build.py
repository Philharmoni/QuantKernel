"""Build and publish the Step 10 negative R2 readiness gate."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract
from core.strategy.r2_quality import evaluate_r2_gate, quality_report


class R2QualityBuilder:
    def __init__(self, config, project_root):
        self.config = config
        self.root = Path(project_root)
        self.step9 = self.root / config["delivery"]["step9_manifest"]

    def build(self):
        manifest = json.loads(self.step9.read_text(encoding="utf-8"))
        decision = json.loads((self.step9.parent / "evidence/promotion_decision.json").read_text(encoding="utf-8"))
        input_manifest = json.loads((self.root / self.config["delivery"]["input_bundle_manifest"]).read_text(encoding="utf-8"))
        table_ids = set(input_manifest.get("tables", {}))
        company_action = "available" if any("company_action" in table for table in table_ids) \
            else self.config["boundaries"]["company_action_ledger"]
        inventory = {
            "r1_evidence_complete": manifest.get("verification", {}).get("passed") is True,
            "champion_available": decision["decision"] != "no_champion_no_promotion" and decision["economic_promoted"],
            "company_action_ledger": company_action,
            "volume_unit_validated": self.config["boundaries"]["volume_unit"] == "validated",
            "board_lot_rules_complete": False,
            "settlement_rules_complete": False,
            "announcement_calendar": "not_available",
            "delisting_mapping_complete": False,
            "real_share_accounting_authorized": False,
        }
        gate = evaluate_r2_gate(inventory)
        evidence = {"step9_manifest_sha256": file_sha256(self.step9),
            "promotion_decision_sha256": file_sha256(self.step9.parent / "evidence/promotion_decision.json"),
            "input_manifest_sha256": file_sha256(self.root / self.config["delivery"]["input_bundle_manifest"]),
            "strategy_inputs_sha256": file_sha256(self.root / "configs/strategy_inputs.yaml"),
            "input_bundle": self.config["delivery"]["active_input_bundle"],
            "confirmation_seen": False, "oos_accessed": False}
        return {"inventory": inventory, "gate": gate, "report": quality_report(gate, inventory), "evidence": evidence}


def default_step10_run_id(project_root=PROJECT_ROOT, use_active=True):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step10_run")
    if use_active and active:
        return active
    identity = {"step": 10, "step9_manifest": file_sha256(project_root / config["delivery"]["step9_manifest"]),
        "input_manifest": file_sha256(project_root / config["delivery"]["input_bundle_manifest"]),
        "code": {name: file_sha256(Path(__file__).with_name(name)) for name in
                 ("r2_quality.py", "r2_quality_build.py")}}
    return config["delivery"]["active_run_group"] + "/step10"


def publish_r2_quality(builder, output_root, run_id):
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 10 output: {output_root}")
    result = builder.build()
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 10 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        files = {}
        for relative, value in (("evidence/r2_input_inventory.json", result["inventory"]),
                                ("evidence/r2_quality_gate.json", result["gate"]),
                                ("evidence/r2_evidence.json", result["evidence"])):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            files[relative] = file_sha256(target)
        target = staging / "reports/r2_quality_report.md"
        target.parent.mkdir(parents=True)
        target.write_text(result["report"], encoding="utf-8")
        files[str(target.relative_to(staging))] = file_sha256(target)
        manifest = {"step": 10, "run_id": run_id,
            "run_hash": "step10_" + canonical_hash(files)[:16], "status": "complete", "quality_gate_executed": True,
            "engineering_passed": True, "r2_quality_gate_passed": result["gate"]["passed"],
            "r2_backtest_permitted": False, "can_enter_step11": False,
            "can_enter_step12_r1_sensitivity": True, "real_share_account_ready": False, "live_ready": False,
            "confirmation_seen": False, "oos_accessed": False, "files": files}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return manifest
    except Exception:
        shutil.rmtree(staging)
        raise
