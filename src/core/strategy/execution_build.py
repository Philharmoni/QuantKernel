"""Build and atomically publish Step 5 execution and R1 account tables."""
import json
import os
from pathlib import Path
import shutil

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract
from core.strategy.inputs import quote_string
from core.strategy.r1_engine import R1Engine

TABLE_KEYS = {
    "execution_eligibility": ["execution_date", "ts_code"],
    "order_event": ["order_id", "event_seq"],
    "fill": ["fill_id"],
    "position_lot": ["valuation_date", "ts_code", "lot_id"],
    "cash_ledger": ["cash_event_id"],
    "nav_daily": ["trade_date", "account_id", "scenario_id"],
    "cost_daily": ["trade_date", "account_id", "scenario_id"],
    "risk_daily": ["trade_date", "account_id", "scenario_id"],
}


class Step5Builder:
    def __init__(self, inputs, step4_run, config):
        self.inputs = inputs
        self.db = inputs.db
        self.config = config
        self.step4_run = Path(step4_run)
        manifest = json.loads((self.step4_run / "manifest.json").read_text(encoding="utf-8"))
        self.step4_manifest = manifest
        for table in ("target_portfolio", "order_intent"):
            entry = manifest["tables"][table]
            if file_sha256(self.step4_run / entry["path"]) != entry["sha256"]:
                raise ValueError(f"Step 4 input hash mismatch: {table}")
        self.order_path = self.step4_run / manifest["tables"]["order_intent"]["path"]
        self.target_path = self.step4_run / manifest["tables"]["target_portfolio"]["path"]
        self.price = inputs.relation("l1/daily_adjusted_price")
        self.state = inputs.relation("l1/daily_stock_state")
        self.status = inputs.relation("l1/daily_trade_status")
        self.calendar = inputs.calendar()

    def facts_provider(self, day, codes):
        if not codes:
            return {}
        values = ",".join(f"({quote_string(code)})" for code in sorted(codes))
        cursor = self.db.execute(f"""WITH requested(ts_code) AS (VALUES {values})
            SELECT r.ts_code,p.open,p.close,p.adj_factor,p.amount,
                   s.is_st,s.is_delisted,s.is_listed,
                   st.quotation_exists,st.limit_price_exists,st.up_limit,st.down_limit,
                   st.is_suspended,st.is_full_day_suspended,st.is_intraday_suspended
            FROM requested r
            LEFT JOIN {self.price} p ON p.trade_date=? AND p.ts_code=r.ts_code
            LEFT JOIN {self.state} s ON s.trade_date=? AND s.ts_code=r.ts_code
            LEFT JOIN {self.status} st ON st.trade_date=? AND st.ts_code=r.ts_code
            ORDER BY r.ts_code""", [day, day, day])
        names = [item[0] for item in cursor.description]
        return {row[0]: dict(zip(names, row)) for row in cursor.fetchall()}

    def build(self, end_date=None):
        order_relation = f"read_parquet({quote_string(str(self.order_path))})"
        target_relation = f"read_parquet({quote_string(str(self.target_path))})"
        cursor = self.db.execute(f"SELECT * FROM {order_relation} ORDER BY planned_execution_date,order_id")
        names = [item[0] for item in cursor.description]
        orders = [dict(zip(names, row)) for row in cursor.fetchall()]
        scores = {(row[0], row[1]): float(row[2]) for row in self.db.execute(
            f"SELECT signal_date,ts_code,score FROM {target_relation} WHERE score IS NOT NULL").fetchall()}
        engine = R1Engine(self.config, self.calendar.dates, self.facts_provider, orders, scores)
        return engine.run(end_date=end_date)


def default_step5_run_id(project_root=PROJECT_ROOT, use_active=True):
    config = load_stage3_contract(project_root)["config"]
    active = config["delivery"].get("active_step5_run")
    if use_active and active:
        return active
    identity = {"step": 5, "step4_manifest": file_sha256(project_root / config["delivery"]["step4_manifest"]),
        "execution": config["execution"], "accounting": config["accounting"], "costs": config["costs"],
        "objective": config["objective"],
        "code": {name: file_sha256(Path(__file__).with_name(name)) for name in
                 ("execution.py", "accounting.py", "r1_engine.py", "execution_build.py")}}
    return config["delivery"]["active_run_group"] + "/step5"


def publish_step5(builder, output_root, run_id, end_date=None):
    import pandas as pd
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"Immutable Step 5 output: {output_root}")
    tables = builder.build(end_date=end_date)
    if "l3/forward_return" in builder.inputs.read_log:
        raise PermissionError("Step 5 attempted to read research labels")
    staging = output_root.with_name("." + output_root.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"Step 5 staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        manifests = {}
        for table, rows in tables.items():
            if not rows:
                raise ValueError(f"Step 5 table is unexpectedly empty: {table}")
            target = staging / ("l4" if table == "execution_eligibility" else "l5") / table / "part-00000.parquet"
            target.parent.mkdir(parents=True)
            key = TABLE_KEYS[table]
            builder.inputs.db.register("stage5_publish_rows", pd.DataFrame(rows))
            builder.inputs.db.execute(f"COPY (SELECT * FROM stage5_publish_rows ORDER BY {','.join(key)}) "
                                      f"TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            builder.inputs.db.unregister("stage5_publish_rows")
            published = f"read_parquet({quote_string(str(target))})"
            count, distinct = builder.inputs.db.execute(
                f"SELECT count(*),count(DISTINCT ROW({','.join(key)})) FROM {published}").fetchone()
            nulls = builder.inputs.db.execute(
                f"SELECT count(*) FROM {published} WHERE {' OR '.join(f'{field} IS NULL' for field in key)}").fetchone()[0]
            if count != distinct or nulls:
                raise ValueError(f"Duplicate or null Step 5 key: {table}")
            schema = {row[0]: row[1] for row in builder.inputs.db.execute(f"DESCRIBE SELECT * FROM {published}").fetchall()}
            manifests[table] = {"path": str(target.relative_to(staging)), "primary_key": key,
                                "rows": count, "schema": schema, "sha256": file_sha256(target)}
            (target.parent / "manifest.json").write_text(json.dumps(manifests[table], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        identity = {"step": 5, "run_id": run_id,
            "run_hash": "step5_" + canonical_hash(manifests)[:16], "status": "complete", "input_bundle": builder.inputs.manifest["bundle_id"],
            "step4_manifest_sha256": file_sha256(builder.step4_run / "manifest.json"),
            "execution_model": "daily_open_proxy_v1", "fill_model": "proxy_not_observed",
            "accounting_mode": "adjusted_unit_proxy", "real_share_account_ready": False,
            "research_labels_read": False, "input_read_log": builder.inputs.read_log, "tables": manifests}
        (staging / "manifest.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(staging, output_root)
        return identity
    except Exception:
        shutil.rmtree(staging)
        raise
