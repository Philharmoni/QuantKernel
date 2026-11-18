"""Verify Step 9 evidence hashes, report/account consistency and no-promotion decision."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import file_sha256, load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.reporting_build import Step9Builder
from core.strategy.reporting_publish import default_step9_run_id, publish_step9


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step9_run_id()
    run_root = root / "runs" / run_id
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    repeat_root = root / "verification" / (run_id + "_repeat")
    run_roots = {step: root / "runs" / config["delivery"][f"active_step{step}_run"] for step in range(3, 9)}
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        if repeat_root.exists():
            repeat = json.loads((repeat_root / "manifest.json").read_text(encoding="utf-8"))
        else:
            repeat = publish_step9(Step9Builder(inputs, config, PROJECT_ROOT, run_roots), repeat_root, repeat_root.name)
        metrics = json.loads((run_root / "evidence/report_metrics.json").read_text(encoding="utf-8"))
        decision = json.loads((run_root / "evidence/promotion_decision.json").read_text(encoding="utf-8"))
        index = json.loads((run_root / "evidence/evidence_index.json").read_text(encoding="utf-8"))
        mismatches = []
        for period, step, period_id in (("design", 7, "design"), ("validation", 8, "validation")):
            rows = fetch_dicts(inputs.db, "SELECT * FROM read_parquet(?) WHERE period_id=?",
                [str(run_roots[step] / "l5/performance/part-00000.parquet"), period_id])
            for account, reported in metrics[period].items():
                actual = next(row for row in rows if row["account_id"] == account)
                for field, value in reported.items():
                    if value is None or actual[field] is None:
                        if value != actual[field]:
                            mismatches.append(f"{period}:{account}:{field}")
                    elif abs(float(value) - float(actual[field])) > 1e-12:
                        mismatches.append(f"{period}:{account}:{field}")
        report_tokens_missing = []
        for account in ("c0_three_module_fixed__executed_proxy_net",):
            for period in ("design", "validation"):
                token = f"{100 * float(metrics[period][account]['cumulative_return']):.4f}%"
                if token not in (run_root / "reports/performance_report.md").read_text(encoding="utf-8"):
                    report_tokens_missing.append(token)
        evidence_hash_mismatches = [path for path, sha in index["reports"].items()
                                    if file_sha256(run_root / path) != sha]
        observations = {
            "repeat_file_mismatches": [path for path, sha in manifest["files"].items()
                                       if repeat["files"].get(path) != sha],
            "report_metric_mismatches": mismatches,
            "report_tokens_missing": report_tokens_missing,
            "evidence_hash_mismatches": evidence_hash_mismatches,
            "decision_mismatch": int(decision["decision"] != "no_champion_no_promotion"
                or decision["economic_promoted"] or decision["r2_backtest_permitted"]
                or decision["real_share_account_ready"] or decision["live_ready"]),
            "forbidden_scope": int(decision["confirmation_seen"] or decision["oos_accessed"]
                or manifest["confirmation_seen"] or manifest["oos_accessed"]),
            "research_labels_read": "l3/forward_return" in manifest["input_read_log"],
            "trial_rows": fetch_dicts(inputs.db, "SELECT count(*) n FROM read_parquet(?)",
                [str(run_root / "l5/trial_ledger/part-00000.parquet")])[0]["n"],
        }
    failures = []
    for name in ("repeat_file_mismatches", "report_metric_mismatches", "report_tokens_missing", "evidence_hash_mismatches"):
        if observations[name]:
            failures.append({"check": name, "value": observations[name]})
    for name in ("decision_mismatch", "forbidden_scope"):
        if observations[name]:
            failures.append({"check": name, "value": observations[name]})
    if observations["research_labels_read"] or observations["trial_rows"] != 14:
        failures.append({"check": "labels_or_trial_coverage"})
    result = {"passed": not failures, "step": 9, "run_id": run_id, "failures": failures,
        "observations": observations, "decision": "no_champion_no_promotion",
        "economic_promoted": False, "can_enter_step10": not failures,
        "r2_backtest_permitted": False, "confirmation_seen": False, "oos_accessed": False,
        "live_ready": False}
    target = run_root / "verification/step9_checks.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step9_checks.json",
                                "sha256": file_sha256(target)}
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
