"""Atomic Step 8 validation publication."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract
from core.strategy.inputs import quote_string
from core.strategy.research_publish import TABLE_KEYS as BASE_KEYS

TABLE_KEYS = dict(BASE_KEYS)
TABLE_KEYS.update({"candidate_freeze": ["strategy_id"],
                   "promotion_gate": ["period_id", "strategy_id", "scenario_id"]})


def default_step8_run_id(project_root=PROJECT_ROOT, use_active=True):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step8_run")
    if use_active and active:
        return active
    identity = {"step": 8, "step7_manifest": file_sha256(project_root / config["delivery"]["step7_manifest"]),
        "preregistration": file_sha256(project_root / config["step8"]["preregistration"]),
        "step8": config["step8"], "code": {name: file_sha256(Path(__file__).with_name(name)) for name in
                 ("promotion.py", "validation_build.py", "validation_publish.py", "research.py",
                  "research_build.py", "benchmark.py", "benchmark_build.py", "r1_engine.py",
                  "execution.py", "accounting.py")}}
    return config["delivery"]["active_run_group"] + "/step8"


def publish_step8(builder, output_root, run_id):
    import pandas as pd
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 8 output: {output_root}")
    tables = builder.build()
    if "l3/forward_return" in builder.inputs.read_log:
        raise PermissionError("Step 8 attempted to read research labels")
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 8 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        manifests = {}
        for table, rows in tables.items():
            if not rows:
                raise ValueError(f"Step 8 table is unexpectedly empty: {table}")
            target = staging / "l5" / table / "part-00000.parquet"
            target.parent.mkdir(parents=True)
            key = TABLE_KEYS[table]
            builder.inputs.db.register("stage8_publish_rows", pd.DataFrame(rows))
            builder.inputs.db.execute(f"COPY (SELECT * FROM stage8_publish_rows ORDER BY {','.join(key)}) "
                                      f"TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            builder.inputs.db.unregister("stage8_publish_rows")
            published = f"read_parquet({quote_string(str(target))})"
            count, distinct = builder.inputs.db.execute(
                f"SELECT count(*),count(DISTINCT ROW({','.join(key)})) FROM {published}").fetchone()
            nulls = builder.inputs.db.execute(
                f"SELECT count(*) FROM {published} WHERE {' OR '.join(f'{field} IS NULL' for field in key)}").fetchone()[0]
            if count != distinct or nulls:
                raise ValueError(f"Duplicate or null Step 8 key: {table}")
            schema = {row[0]: row[1] for row in builder.inputs.db.execute(f"DESCRIBE SELECT * FROM {published}").fetchall()}
            manifests[table] = {"path": str(target.relative_to(staging)), "primary_key": key,
                                "rows": count, "schema": schema, "sha256": file_sha256(target)}
            (target.parent / "manifest.json").write_text(json.dumps(manifests[table], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        gate = tables["promotion_gate"][0]
        identity = {"step": 8, "run_id": run_id,
            "run_hash": "step8_" + canonical_hash(manifests)[:16], "status": "complete", "input_bundle": builder.inputs.manifest["bundle_id"],
            "candidate_id": builder.config["step8"]["candidate_id"], "period_id": "validation",
            "validation_period": builder.config["research_protocol"]["validation_period"],
            "preregistration_sha256": file_sha256(builder.preregistration_path),
            "promotion_status": gate["promotion_status"], "research_verification_passed": gate["research_verification_passed"],
            "validation_seen": True, "confirmation_seen": False, "oos_accessed": False,
            "economic_promoted": False, "real_share_account_ready": False, "live_ready": False,
            "research_labels_read": False, "input_read_log": builder.inputs.read_log, "tables": manifests}
        (staging / "manifest.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return identity
    except Exception:
        shutil.rmtree(staging)
        raise
