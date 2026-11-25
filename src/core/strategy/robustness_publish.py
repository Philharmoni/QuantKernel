"""Atomic Step 13 R1 robustness publication."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract
from core.strategy.inputs import quote_string


TABLE_KEYS = {
    "robustness_account_nav": ["trade_date", "robustness_id"],
    "truncation_comparison": ["comparison_id"],
    "pit_access_audit": ["check_id"],
    "stale_pressure_daily": ["trade_date"],
    "synthetic_position_pressure": ["valuation_date", "ts_code", "lot_id", "scenario_id"],
    "robustness_registry": ["check_id"],
}


def default_step13_run_id(project_root=PROJECT_ROOT, use_active=True):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step13_run")
    if use_active and active:
        return active
    identity = {"step": 13,
        "step5_manifest": file_sha256(project_root / config["delivery"]["step5_manifest"]),
        "step7_manifest": file_sha256(project_root / config["delivery"]["step7_manifest"]),
        "step13": config["step13"], "costs": config["costs"], "execution": config["execution"],
        "accounting": config["accounting"],
        "code": {name: file_sha256(Path(__file__).with_name(name)) for name in
                 ("robustness.py", "robustness_build.py", "robustness_publish.py", "r1_engine.py",
                  "execution.py", "accounting.py")}}
    return config["delivery"]["active_run_group"] + "/step13"


def publish_step13(builder, output_root, run_id):
    import pandas as pd
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 13 output: {output_root}")
    tables = builder.build()
    if "l3/forward_return" in builder.inputs.read_log:
        raise PermissionError("Step 13 attempted to read research labels")
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 13 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        manifests = {}
        for table, rows in tables.items():
            if not rows:
                raise ValueError(f"Step 13 table is unexpectedly empty: {table}")
            target = staging / "l5" / table / "part-00000.parquet"
            target.parent.mkdir(parents=True)
            key = TABLE_KEYS[table]
            builder.inputs.db.register("stage13_publish_rows", pd.DataFrame(rows))
            builder.inputs.db.execute(f"COPY (SELECT * FROM stage13_publish_rows ORDER BY {','.join(key)}) "
                                      f"TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            builder.inputs.db.unregister("stage13_publish_rows")
            published = f"read_parquet({quote_string(str(target))})"
            count, distinct = builder.inputs.db.execute(
                f"SELECT count(*),count(DISTINCT ROW({','.join(key)})) FROM {published}").fetchone()
            nulls = builder.inputs.db.execute(
                f"SELECT count(*) FROM {published} WHERE {' OR '.join(f'{field} IS NULL' for field in key)}").fetchone()[0]
            if count != distinct or nulls:
                raise ValueError(f"Duplicate or null Step 13 key: {table}")
            schema = {row[0]: row[1] for row in builder.inputs.db.execute(f"DESCRIBE SELECT * FROM {published}").fetchall()}
            manifests[table] = {"path": str(target.relative_to(staging)), "primary_key": key,
                                "rows": count, "schema": schema, "sha256": file_sha256(target)}
            (target.parent / "manifest.json").write_text(
                json.dumps(manifests[table], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest = {"step": 13, "run_id": run_id,
            "run_hash": "step13_" + canonical_hash(manifests)[:16], "status": "complete", "period_id": "design",
            "design_period": builder.config["research_protocol"]["design_period"],
            "truncation_date": builder.cutoff, "future_quote_multiplier": builder.multiplier,
            "input_bundle": builder.inputs.manifest["bundle_id"],
            "step5_manifest_sha256": file_sha256(builder.step5_run / "manifest.json"),
            "step7_manifest_sha256": file_sha256(builder.step7_run / "manifest.json"),
            "accounting_mode": "adjusted_unit_proxy", "economic_promoted": False,
            "r2_backtest_permitted": False, "can_enter_step11": False,
            "validation_seen": False, "confirmation_seen": False, "oos_accessed": False,
            "research_labels_read": False, "input_read_log": builder.inputs.read_log,
            "can_enter_step14": True, "tables": manifests}
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return manifest
    except Exception:
        shutil.rmtree(staging)
        raise
