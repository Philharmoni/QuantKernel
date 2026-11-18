"""Verify the Step 10 negative R2 gate and absence of R2 artifacts."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import file_sha256, load_stage3_contract
from core.strategy.r2_quality_build import R2QualityBuilder, default_step10_run_id, publish_r2_quality


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step10_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    if repeat_root.exists():
        repeat = json.loads((repeat_root / "manifest.json").read_text(encoding="utf-8"))
    else:
        repeat = publish_r2_quality(R2QualityBuilder(config, PROJECT_ROOT), repeat_root, repeat_root.name)
    gate = json.loads((run_root / "evidence/r2_quality_gate.json").read_text(encoding="utf-8"))
    observations = {
        "repeat_file_mismatches": [path for path, sha in manifest["files"].items() if repeat["files"].get(path) != sha],
        "required_blockers_missing": sorted({"champion_available", "company_action_ledger", "volume_units_validated"} - set(gate["blockers"])),
        "r2_permission_mismatch": int(gate["passed"] or gate["r2_backtest_permitted"] or gate["can_enter_step11"]
            or manifest["r2_backtest_permitted"] or manifest["can_enter_step11"]),
        "r2_artifact_rows": sum(1 for path in run_root.rglob("*") if path.is_file() and ("l6" in path.parts or "real_share" in path.name)),
        "forbidden_scope": int(manifest["confirmation_seen"] or manifest["oos_accessed"]),
        "evidence_hash_mismatch": int(json.loads((run_root / "evidence/r2_evidence.json").read_text())["step9_manifest_sha256"]
            != file_sha256(PROJECT_ROOT / config["delivery"]["step9_manifest"])),
    }
    failures = [{name: value} for name, value in observations.items() if value]
    result = {"passed": not failures, "step": 10, "run_id": run_id, "failures": failures,
        "observations": observations, "quality_gate_executed": True,
        "r2_quality_gate_passed": False, "r2_backtest_permitted": False, "can_enter_step11": False,
        "can_enter_step12_r1_sensitivity": not failures, "real_share_account_ready": False, "live_ready": False}
    target = run_root / "verification/step10_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step10_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
