"""Atomic Step 7 design-research publication."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.strategy.benchmark_publish import TABLE_KEYS as BASE_KEYS
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract
from core.strategy.inputs import quote_string

TABLE_KEYS = dict(BASE_KEYS)
TABLE_KEYS.update({
    "monthly_return": ["period_id", "account_id", "scenario_id"],
    "execution_summary": ["period_id", "strategy_id", "scenario_id"],
    "exposure_daily": ["trade_date", "strategy_id"],
    "research_trial": ["research_activity_id", "candidate_id"],
})


def default_step7_run_id(project_root=PROJECT_ROOT, use_active=True):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step7_run")
    if use_active and active:
        return active
    identity = {"step": 7, "step6_manifest": file_sha256(project_root / config["delivery"]["step6_manifest"]),
        "step5_manifest": file_sha256(project_root / config["delivery"]["step5_manifest"]),
        "research_protocol": config["research_protocol"], "step7": config["step7"],
        "benchmarks": config["benchmarks"],
        "costs": config["costs"], "execution": config["execution"], "accounting": config["accounting"],
        "code": {name: file_sha256(Path(__file__).with_name(name)) for name in
                 ("research.py", "research_build.py", "research_publish.py", "benchmark.py",
                  "benchmark_build.py", "r1_engine.py", "execution.py", "accounting.py")}}
    return "step7_" + canonical_hash(identity)[:16]


def publish_step7(builder, output_root, run_id):
    import pandas as pd
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 7 output: {output_root}")
    tables = builder.build()
    if "l3/forward_return" in builder.inputs.read_log:
        raise PermissionError("Step 7 attempted to read research labels")
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 7 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        manifests = {}
        for table, rows in tables.items():
            if not rows:
                raise ValueError(f"Step 7 table is unexpectedly empty: {table}")
            target = staging / "l5" / table / "part-00000.parquet"
            target.parent.mkdir(parents=True)
            key = TABLE_KEYS[table]
            builder.inputs.db.register("stage7_publish_rows", pd.DataFrame(rows))
            builder.inputs.db.execute(f"COPY (SELECT * FROM stage7_publish_rows ORDER BY {','.join(key)}) "
                                      f"TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            builder.inputs.db.unregister("stage7_publish_rows")
            published = f"read_parquet({quote_string(str(target))})"
            count, distinct = builder.inputs.db.execute(
                f"SELECT count(*),count(DISTINCT ROW({','.join(key)})) FROM {published}").fetchone()
            nulls = builder.inputs.db.execute(
                f"SELECT count(*) FROM {published} WHERE {' OR '.join(f'{field} IS NULL' for field in key)}").fetchone()[0]
            if count != distinct or nulls:
                raise ValueError(f"Duplicate or null Step 7 key: {table}")
            schema = {row[0]: row[1] for row in builder.inputs.db.execute(f"DESCRIBE SELECT * FROM {published}").fetchall()}
            manifests[table] = {"path": str(target.relative_to(staging)), "primary_key": key,
                                "rows": count, "schema": schema, "sha256": file_sha256(target)}
            (target.parent / "manifest.json").write_text(json.dumps(manifests[table], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        identity = {"step": 7, "run_id": run_id, "status": "complete", "input_bundle": builder.inputs.manifest["bundle_id"],
            "period_id": "design", "design_period": builder.config["research_protocol"]["design_period"],
            "completed_candidates": ["c0_three_module_fixed"], "diagnostics_run": [],
            "validation_seen": False, "confirmation_seen": False, "oos_accessed": False,
            "accounting_mode": "adjusted_unit_proxy", "economic_promoted": False,
            "research_labels_read": False, "input_read_log": builder.inputs.read_log, "tables": manifests}
        (staging / "manifest.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return identity
    except Exception:
        shutil.rmtree(staging)
        raise
