"""Prepare and consume immutable, physically bounded Stage 3 inputs."""
import json
import os
from collections import OrderedDict
from pathlib import Path
import re
import shutil
import threading

import duckdb

from core.config.paths import PROJECT_ROOT
from core.data.store import factor_partition_relation, stored_artifact_sha256
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract, parse_ymd
from core.quality.verification import verification_mode

RESEARCH_ONLY = "l3/forward_return"
PROTECTED_MIXED = {"l3/factor_value", "l3/factor_processed"}
SAFE_NAME = re.compile(r"^[a-z0-9_]+$")


def load_input_spec(project_root=PROJECT_ROOT):
    import yaml
    path = project_root / "configs/strategy_inputs.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def quote_name(name):
    if not SAFE_NAME.fullmatch(name):
        raise ValueError(f"Unsafe identifier: {name}")
    return '"' + name + '"'


def quote_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def connect(engine, temp_directory):
    temp_directory.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect()
    db.execute(f"SET threads={int(engine['threads'])}")
    db.execute(f"SET memory_limit={quote_string(engine['memory_limit'])}")
    db.execute(f"SET temp_directory={quote_string(str(temp_directory))}")
    return db


def bounded_calendar(rows, cutoff, fixture=False):
    parsed_cutoff = parse_ymd(cutoff)
    if not rows:
        raise ValueError("Bounded calendar has no rows")
    selected = []
    for row in rows:
        value = row.get("trade_date", row.get("cal_date"))
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        day = parse_ymd(value)
        if day <= parsed_cutoff:
            selected.append((day, row))
    selected.sort(key=lambda item: item[0])
    if not selected or len({day for day, _ in selected}) != len(selected):
        raise ValueError("Bounded calendar is empty or has duplicate dates")
    result = []
    for index, (day, row) in enumerate(selected):
        original_index = row.get("trade_index")
        if original_index is not None and int(original_index) != index + (0 if fixture else 1):
            raise ValueError(f"Calendar trade_index mismatch at {day}")
        result.append({"trade_date": day, "trade_index": int(original_index) if original_index is not None else index + 1,
                       "prev_trade_date": selected[index - 1][0] if index else None,
                       "next_trade_date": selected[index + 1][0] if index + 1 < len(selected) else None,
                       "calendar_start": selected[0][0], "calendar_end": parsed_cutoff,
                       "exchange": row.get("exchange", "SSE")})
    return result


class BoundedCalendar:
    def __init__(self, rows, cutoff):
        self.rows = bounded_calendar(rows, cutoff, fixture=rows[0].get("is_open") is not None)
        self.dates = tuple(row["trade_date"] for row in self.rows)
        self.indices = {row["trade_date"]: row["trade_index"] for row in self.rows}

    def next(self, value):
        day = parse_ymd(value)
        eligible = [date for date in self.dates if date > day]
        if not eligible:
            raise PermissionError(f"No authorized next trading day after {day}")
        return eligible[0]

    def offset(self, value, number):
        day = parse_ymd(value)
        index = self.dates.index(day) + number
        if not 0 <= index < len(self.dates):
            raise PermissionError(f"Trading date offset leaves authorized snapshot: {day} {number}")
        return self.dates[index]


def prepare_snapshot(project_root=PROJECT_ROOT):
    contract = load_stage3_contract(project_root)["config"]
    spec = load_input_spec(project_root)
    cutoff = spec["cutoff"]
    if cutoff != contract["permissions"]["in_sample_end"]:
        raise PermissionError("Input cutoff differs from frozen in-sample cutoff")
    output_root = project_root / contract["delivery"]["root"] / "inputs"
    output_root.mkdir(parents=True, exist_ok=True)
    work = output_root / ".staging"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    db = connect(spec["engine"], work / "duckdb_tmp")
    tables = {}
    try:
        for artifact, rules in spec["source_tables"].items():
            layer, name = artifact.split("/")
            source_root = project_root / ("factor_research" if layer in {"l3", "l4"} else "data_middle")
            source_dir = source_root / layer / name
            source_path = source_dir / "part-00000.parquet"
            source_manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
            actual_hash = stored_artifact_sha256(source_dir)
            if actual_hash != source_manifest["sha256"] or source_manifest["primary_key"] != rules["key"]:
                raise ValueError(f"Source integrity/key mismatch: {artifact}")
            relation = (factor_partition_relation(source_dir)
                        if source_manifest.get("storage_layout") == "factor_id_partitioned_v1"
                        else f"read_parquet({quote_string(str(source_path))})")
            columns = db.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
            available = {row[0]: row[1] for row in columns}
            selected = rules.get("columns", list(available))
            missing = set(selected + rules["dates"] + rules["key"] + rules.get("required_columns", [])) - set(available)
            if missing:
                raise ValueError(f"Missing input columns {artifact}: {sorted(missing)}")
            selected = list(dict.fromkeys(selected + rules["dates"] + rules["key"]))
            expressions = []
            cutoff_sql = quote_string(parse_ymd(cutoff).isoformat())
            quarantine = "FALSE"
            quarantined = 0
            if rules.get("quarantine_cross_cutoff_labels"):
                quarantine = f"(entry_date>DATE {cutoff_sql} OR exit_date>DATE {cutoff_sql})"
                quarantined = db.execute(f"SELECT count(*) FROM {relation} WHERE trade_date<=DATE {cutoff_sql} AND {quarantine}").fetchone()[0]
            for column in selected:
                field = quote_name(column)
                if column == "next_trade_date":
                    expressions.append(f"CASE WHEN {field}<=DATE {cutoff_sql} THEN {field} ELSE NULL END AS {field}")
                elif column in ("calendar_start", "calendar_end"):
                    expressions.append(f"DATE {quote_string(parse_ymd(cutoff if column == 'calendar_end' else contract['permissions']['calendar_start']).isoformat())} AS {field}")
                elif rules.get("quarantine_cross_cutoff_labels") and column in ("entry_date", "exit_date", "planned_exit_date", "entry_price", "exit_price", "ret"):
                    cast = f"::{available[column]}" if column in ("entry_price", "exit_price", "ret") else "::DATE"
                    expressions.append(f"CASE WHEN {quarantine} THEN NULL{cast} ELSE {field} END AS {field}")
                elif rules.get("quarantine_cross_cutoff_labels") and column == "exception_flag":
                    expressions.append(f"CASE WHEN {quarantine} THEN 'input_scope_quarantined' ELSE {field} END AS {field}")
                else:
                    expressions.append(field)
            if rules.get("quarantine_cross_cutoff_labels"):
                expressions.append(f"CASE WHEN {quarantine} THEN 'quarantined' ELSE 'admitted' END AS input_scope_status")
            predicates = []
            for column in rules["dates"]:
                if column in rules.get("null_dates", []):
                    predicates.append(f"({quote_name(column)} IS NULL OR {quote_name(column)}<=DATE {quote_string(parse_ymd(cutoff).isoformat())})")
                elif rules.get("keep_null_actual_dates") and column != "trade_date":
                    continue
                elif rules.get("cutoff_null"):
                    predicates.append(f"({quote_name(column)} IS NULL OR {quote_name(column)}<=DATE {quote_string(parse_ymd(cutoff).isoformat())})")
                else:
                    predicates.append(f"{quote_name(column)}<=DATE {quote_string(parse_ymd(cutoff).isoformat())}")
            # Quarantined in-sample signals keep a null label and explicit scope status; OOS signal rows are not copied.
            if rules.get("enforce_realized_before_trade_date"):
                violations = db.execute(f"SELECT count(*) FROM {relation} WHERE last_exit_date>=trade_date").fetchone()[0]
                if violations:
                    raise PermissionError(f"Unrealized composite weights: {violations}")
            query = f"SELECT {','.join(expressions)} FROM {relation} WHERE {' AND '.join(predicates)} ORDER BY {','.join(map(quote_name, rules['key']))}"
            domain = "research_only" if artifact == RESEARCH_ONLY else "signal_inputs"
            target = work / domain / name / "part-00000.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            db.execute(f"COPY ({query}) TO {quote_string(str(target))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            output_relation = f"read_parquet({quote_string(str(target))})"
            count, distinct = db.execute(f"SELECT count(*),count(DISTINCT ROW({','.join(map(quote_name, rules['key']))})) FROM {output_relation}").fetchone()
            if count != distinct:
                raise ValueError(f"Duplicate snapshot key: {artifact}")
            nulls = db.execute(f"SELECT count(*) FROM {output_relation} WHERE {' OR '.join(f'{quote_name(key)} IS NULL' for key in rules['key'])}").fetchone()[0]
            if nulls:
                raise ValueError(f"Null snapshot key: {artifact}")
            dates = {}
            for column in rules["dates"]:
                if column in selected:
                    low, high = db.execute(f"SELECT min({quote_name(column)}),max({quote_name(column)}) FROM {output_relation}").fetchone()
                    if high is not None and high > parse_ymd(cutoff):
                        raise PermissionError(f"Snapshot date exceeds cutoff: {artifact}.{column}")
                    dates[column] = {"min": str(low) if low is not None else None, "max": str(high) if high is not None else None}
            schema = {row[0]: row[1] for row in db.execute(f"DESCRIBE SELECT * FROM {output_relation}").fetchall()}
            tables[artifact] = {"domain": domain, "path": f"{domain}/{name}/part-00000.parquet",
                "key": rules["key"], "rows": count, "schema": schema, "sha256": file_sha256(target),
                "source": {"manifest_sha256": file_sha256(source_dir / "manifest.json"), "parquet_sha256": actual_hash,
                           "lineage": "incremental_mixed_accepted" if artifact in PROTECTED_MIXED else "strict_current_file",
                           "upstream": source_manifest.get("upstream", {})}, "dates": dates,
                "preparation_full_source_read": True, "quarantined_cross_cutoff_labels": quarantined}
            print(f"snapshot {artifact}: {count} rows", flush=True)
    except Exception:
        db.close()
        shutil.rmtree(work)
        raise
    db.close()
    identity = {"version": 1, "mode": "physical_in_sample_snapshot", "cutoff": cutoff,
                "config_sha256": canonical_hash(spec), "tables": tables}
    bundle_id = "in_sample_" + canonical_hash(identity)[:16]
    target_root = output_root / bundle_id
    if target_root.exists():
        shutil.rmtree(work)
        raise FileExistsError(f"Immutable bundle already exists: {bundle_id}")
    for artifact, entry in tables.items():
        source = work / entry["path"]
        target = target_root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
    manifest = dict(identity, bundle_id=bundle_id, status="complete", strategy_input_root="signal_inputs",
                    preparation_authorization="independent_physical_snapshot_no_oos_performance",
                    units=spec["units"], capabilities=spec["capabilities"])
    (target_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
    from core.strategy.admission import record_snapshot_admission
    validation = record_snapshot_admission(target_root, project_root)
    if not validation["passed"]:
        shutil.rmtree(work)
        raise RuntimeError(f"Snapshot admission failed: {validation['failures']}")
    pointer = output_root / "active_input.json"
    temporary = output_root / ".active_input.json.tmp"
    temporary.write_text(json.dumps({"version": 1, "bundle_id": bundle_id},
                                    ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, pointer)
    shutil.rmtree(work)
    return json.loads((target_root / "manifest.json").read_text(encoding="utf-8"))


class SnapshotInputs:
    _content_cache = OrderedDict()
    _content_cache_lock = threading.Lock()
    _content_cache_size = 32

    def __init__(self, bundle_root, project_root=PROJECT_ROOT, verification="standard"):
        self.root = Path(bundle_root).resolve()
        self.project_root = Path(project_root).resolve()
        self.verification = verification_mode(verification)
        if not self.root.is_relative_to(self.project_root / "strategy_research/inputs") and not self.root.is_relative_to(self.project_root / "tests/fixtures/golden_stage3"):
            raise PermissionError("Snapshot root is outside the authorized input namespace")
        manifest_path = self.root / "manifest.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("status") != "complete" and not self.manifest.get("fictional"):
            raise PermissionError("Input bundle is not complete")
        self.cutoff = self.manifest.get("cutoff", "20241231")
        admission_digest = None
        admission = self.manifest.get("admission")
        if admission:
            admission_digest = file_sha256(self.root / admission["path"])
            if not admission.get("passed") or admission_digest != admission["sha256"]:
                raise ValueError("Bundle admission evidence is failed or has a hash mismatch")
        self._cache_key = (str(self.root), file_sha256(manifest_path), admission_digest)
        self._content_verified = False
        if not self.manifest.get("fictional"):
            self._ensure_content_verified(refresh=self.verification == "audit")
        self.db = duckdb.connect()
        self.read_log = []

    def _ensure_content_verified(self, refresh=False):
        if self.verification == "fast" or self._content_verified:
            return
        with self._content_cache_lock:
            cached = self._cache_key in self._content_cache and not refresh
            if cached:
                self._content_cache.move_to_end(self._cache_key)
        if cached:
            self._content_verified = True
            return
        for artifact, entry in self.manifest["tables"].items():
            if file_sha256(self.root / entry["path"]) != entry["sha256"]:
                raise ValueError(f"Bundle file hash mismatch at admission: {artifact}")
        with self._content_cache_lock:
            self._content_cache[self._cache_key] = True
            self._content_cache.move_to_end(self._cache_key)
            while len(self._content_cache) > self._content_cache_size:
                self._content_cache.popitem(last=False)
        self._content_verified = True

    @classmethod
    def clear_verification_cache(cls):
        with cls._content_cache_lock:
            cls._content_cache.clear()

    @classmethod
    def from_fixture(cls, root, project_root=PROJECT_ROOT, verification="standard"):
        root = Path(root)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest.update(status="complete", cutoff="20241231", tables={})
        for path in sorted(root.glob("*.csv")):
            artifact = "l1/daily_calendar" if path.stem == "daily_calendar" else {
                "daily_adjusted_price": "l1/daily_adjusted_price", "daily_stock_state": "l1/daily_stock_state",
                "daily_trade_status": "l1/daily_trade_status", "financial_available": "l1/financial_available",
                "daily_research_universe": "l2/daily_research_universe", "daily_industry": "l3/daily_industry",
                "daily_index_member": "l3/daily_index_member", "daily_benchmark_return": "l3/daily_benchmark_return",
                "factor_value": "l3/factor_value", "factor_processed": "l3/factor_processed",
                "forward_return_research_only": RESEARCH_ONLY}.get(path.stem, path.stem)
            manifest["tables"][artifact] = {"path": path.name, "sha256": manifest["files"][path.name],
                "domain": "research_only" if artifact == RESEARCH_ONLY else "signal_inputs"}
        instance = cls.__new__(cls)
        instance.root = root.resolve()
        instance.project_root = Path(project_root).resolve()
        instance.verification = verification_mode(verification)
        instance.manifest = manifest
        instance.cutoff = manifest["cutoff"]
        instance._cache_key = (str(instance.root), canonical_hash(manifest), None)
        instance._content_verified = False
        instance.db = duckdb.connect()
        instance.read_log = []
        return instance

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def relation(self, artifact, domain="signal_inputs"):
        if artifact not in self.manifest["tables"] or self.manifest["tables"][artifact]["domain"] != domain:
            raise PermissionError(f"Table/domain not admitted: {artifact} {domain}")
        entry = self.manifest["tables"][artifact]
        path = self.root / entry["path"]
        self._ensure_content_verified()
        self.read_log.append(artifact)
        reader = "read_csv" if path.suffix == ".csv" else "read_parquet"
        return f"{reader}({quote_string(str(path))})"

    def calendar(self):
        relation = self.relation("l1/daily_calendar")
        rows = self.db.execute(f"SELECT * FROM {relation} ORDER BY trade_date").fetchall()
        names = [row[0] for row in self.db.description]
        return BoundedCalendar([dict(zip(names, row)) for row in rows], self.cutoff)
