"""Atomic Step 9 report/evidence publication."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract
from core.strategy.inputs import quote_string


def default_step9_run_id(project_root=PROJECT_ROOT, use_active=True):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step9_run")
    if use_active and active:
        return active
    identity = {"step": 9, "step8_manifest": file_sha256(project_root / config["delivery"]["step8_manifest"]),
        "preregistration": file_sha256(project_root / config["step8"]["preregistration"]),
        "registry": file_sha256(project_root / "configs/strategy_registry.yaml"),
        "code": {name: file_sha256(Path(__file__).with_name(name)) for name in
                 ("reporting_build.py", "reporting_publish.py")}}
    return "step9_" + canonical_hash(identity)[:16]


def publish_step9(builder, output_root, run_id):
    import pandas as pd
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 9 output: {output_root}")
    result = builder.build()
    if "l3/forward_return" in builder.inputs.read_log:
        raise PermissionError("Step 9 attempted to read research labels")
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 9 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        files = {}
        for name, content in result["reports"].items():
            target = staging / "reports" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            files[str(target.relative_to(staging))] = file_sha256(target)
        evidence = staging / "evidence"
        evidence.mkdir()
        for name, value in (("report_metrics.json", result["metrics"]),
                            ("promotion_decision.json", result["decision"])):
            target = evidence / name
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
            files[str(target.relative_to(staging))] = file_sha256(target)
        index = builder.evidence_index(files)
        target = evidence / "evidence_index.json"
        target.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        files[str(target.relative_to(staging))] = file_sha256(target)
        rows = result["trial_ledger"]
        target = staging / "l5/trial_ledger/part-00000.parquet"
        target.parent.mkdir(parents=True)
        builder.inputs.db.register("stage9_trial_rows", pd.DataFrame(rows))
        builder.inputs.db.execute(f"COPY (SELECT * FROM stage9_trial_rows ORDER BY research_activity_id,candidate_id,source_step) "
                                  f"TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
        builder.inputs.db.unregister("stage9_trial_rows")
        count, distinct = builder.inputs.db.execute(f"SELECT count(*),count(DISTINCT ROW(research_activity_id,candidate_id,source_step)) "
            f"FROM read_parquet({quote_string(str(target))})").fetchone()
        if count != distinct or count != 14:
            raise ValueError("Step 9 trial ledger key or coverage mismatch")
        files[str(target.relative_to(staging))] = file_sha256(target)
        manifest = {"step": 9, "run_id": run_id, "status": "complete", "candidate_id": "c0_three_module_fixed",
            "decision": "no_champion_no_promotion", "input_bundle": builder.inputs.manifest["bundle_id"],
            "economic_promoted": False, "r2_backtest_permitted": False, "real_share_account_ready": False,
            "live_ready": False, "confirmation_seen": False, "oos_accessed": False,
            "research_labels_read": False, "input_read_log": builder.inputs.read_log,
            "can_enter_step10": True, "files": files}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return manifest
    except Exception:
        shutil.rmtree(staging)
        raise
