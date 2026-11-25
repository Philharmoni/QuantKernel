"""Independently repeat and verify the Step 14 R1 closeout package."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.closeout import REQUIRED_STEPS
from core.strategy.closeout_build import Step14Builder, default_step14_run_id, publish_step14
from core.strategy.contract import file_sha256, load_stage3_contract
from core.strategy.verification import file_repeat_mismatches, repeat_metadata, resolve_repeat
from core.quality.verification import verification_mode


def main():
    mode = verification_mode()
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / config["delivery"]["root"]
    run_id = default_step14_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    repeat, repeat_status = resolve_repeat(repeat_root, lambda: publish_step14(
        Step14Builder(config, PROJECT_ROOT, verification=mode), repeat_root, repeat_root.name), mode)
    closeout = json.loads((run_root / "evidence/r1_closeout.json").read_text(encoding="utf-8"))
    evidence = json.loads((run_root / "evidence/r1_evidence_index.json").read_text(encoding="utf-8"))
    matrix = json.loads((run_root / "evidence/r1_gate_matrix.json").read_text(encoding="utf-8"))
    source_hash_mismatches = []
    for entry in evidence["runs"].values():
        for kind in ("manifest", "verification"):
            if file_sha256(PROJECT_ROOT / entry[f"{kind}_path"]) != entry[f"{kind}_sha256"]:
                source_hash_mismatches.append(f"{entry['run_id']}:{kind}")
    report = (run_root / "reports/r1_closeout_report.md").read_text(encoding="utf-8")
    expected_tokens = ["no_champion_no_promotion", "R2", "正式 OOS"]
    for period in ("design", "validation"):
        expected_tokens.append(f"{100 * float(closeout['key_results'][period]['cumulative_return']):.4f}%")
    observations = {
        "repeat_file_mismatches": file_repeat_mismatches(manifest, repeat),
        "published_file_hash_mismatches": [path for path, sha in manifest["files"].items()
                                           if file_sha256(run_root / path) != sha],
        "source_hash_mismatches": source_hash_mismatches,
        "required_step_mismatch": int(set(evidence["runs"]) != {str(step) for step in REQUIRED_STEPS}),
        "gate_failures": [row["gate_id"] for row in matrix if row["passed"] is False],
        "gate_count_gap": abs(12 - len(matrix)),
        "report_tokens_missing": [token for token in expected_tokens if token not in report],
        "status_mismatch": int(not closeout["r1_research_complete"] or not closeout["engineering_passed"]
            or closeout["economic_promoted"] or closeout["r2_backtest_permitted"]
            or closeout["can_enter_step11"] or closeout["real_share_account_ready"] or closeout["live_ready"]),
        "forbidden_scope": int(closeout["confirmation_seen"] or closeout["oos_accessed"]
            or evidence["confirmation_seen"] or evidence["oos_accessed"] or manifest["data_rows_read"] != 0),
        "r2_artifact_rows": sum(1 for path in run_root.rglob("*")
            if path.is_file() and ("l6" in path.parts or "real_share" in path.name)),
    }
    failures = [{"check": name, "value": value} for name, value in observations.items() if value]
    result = {"passed": not failures, "step": 14, "run_id": run_id, "failures": failures,
        "observations": observations, "r1_research_complete": not failures,
        "engineering_passed": not failures, "economic_promoted": False,
        "r2_backtest_permitted": False, "can_enter_step11": False,
        "confirmation_seen": False, "oos_accessed": False, "live_ready": False,
        **repeat_metadata(mode, repeat_status)}
    target = run_root / "verification/step14_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step14_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
