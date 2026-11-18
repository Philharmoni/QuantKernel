"""Atomic Step 6 publication and content-derived run identity."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract
from core.strategy.inputs import quote_string

TABLE_KEYS = {
    "benchmark_target": ["signal_date", "ts_code", "benchmark_id"],
    "account_nav_daily": ["trade_date", "account_id", "scenario_id"],
    "performance": ["period_id", "account_id", "scenario_id"],
    "relative_performance": ["period_id", "strategy_id", "benchmark_id", "scenario_id"],
    "attribution": ["period_id", "family_id", "transition_id", "scenario_id"],
}


def default_step6_run_id(project_root=PROJECT_ROOT, use_active=True):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step6_run")
    if use_active and active:
        return active
    identity = {"step": 6, "step5_manifest": file_sha256(project_root / config["delivery"]["step5_manifest"]),
        "step4_manifest": file_sha256(project_root / config["delivery"]["step4_manifest"]),
        "step6": config["step6"], "benchmarks": config["benchmarks"], "costs": config["costs"],
        "execution": config["execution"], "accounting": config["accounting"],
        "code": {name: file_sha256(Path(__file__).with_name(name)) for name in
                 ("benchmark.py", "benchmark_build.py", "benchmark_publish.py", "r1_engine.py",
                  "execution.py", "accounting.py")}}
    return "step6_" + canonical_hash(identity)[:16]


def publish_step6(builder, output_root, run_id, end_date=None, perturb_after=None):
    import pandas as pd
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 6 output: {output_root}")
    tables = builder.build(end_date=end_date, perturb_after=perturb_after)
    if "l3/forward_return" in builder.inputs.read_log:
        raise PermissionError("Step 6 attempted to read research labels")
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 6 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        manifests = {}
        for table, rows in tables.items():
            if not rows:
                raise ValueError(f"Step 6 table is unexpectedly empty: {table}")
            target = staging / "l5" / table / "part-00000.parquet"
            target.parent.mkdir(parents=True)
            key = TABLE_KEYS[table]
            builder.inputs.db.register("stage6_publish_rows", pd.DataFrame(rows))
            builder.inputs.db.execute(f"COPY (SELECT * FROM stage6_publish_rows ORDER BY {','.join(key)}) "
                                      f"TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            builder.inputs.db.unregister("stage6_publish_rows")
            published = f"read_parquet({quote_string(str(target))})"
            count, distinct = builder.inputs.db.execute(
                f"SELECT count(*),count(DISTINCT ROW({','.join(key)})) FROM {published}").fetchone()
            nulls = builder.inputs.db.execute(
                f"SELECT count(*) FROM {published} WHERE {' OR '.join(f'{field} IS NULL' for field in key)}").fetchone()[0]
            if count != distinct or nulls:
                raise ValueError(f"Duplicate or null Step 6 key: {table}")
            schema = {row[0]: row[1] for row in builder.inputs.db.execute(f"DESCRIBE SELECT * FROM {published}").fetchall()}
            manifests[table] = {"path": str(target.relative_to(staging)), "primary_key": key,
                                "rows": count, "schema": schema, "sha256": file_sha256(target)}
            (target.parent / "manifest.json").write_text(json.dumps(manifests[table], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        identity = {"step": 6, "run_id": run_id, "status": "complete", "input_bundle": builder.inputs.manifest["bundle_id"],
            "step5_manifest_sha256": file_sha256(builder.step5_run / "manifest.json"),
            "small_window": builder.config["step6"]["small_window"], "accounting_mode": "adjusted_unit_proxy",
            "real_share_account_ready": False, "research_labels_read": False,
            "input_read_log": builder.inputs.read_log, "tables": manifests}
        (staging / "manifest.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return identity
    except Exception:
        shutil.rmtree(staging)
        raise
