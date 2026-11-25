"""Read raw files through DuckDB; publish beneath stage-specific output roots."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil

import duckdb

from core.config import Paths, load_paths
from core.config.settings import load_settings


def qi(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def qs(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def date_sql(expression: str) -> str:
    text = f"regexp_replace(trim(cast({expression} AS VARCHAR)), '\\.0$', '')"
    return (f"CASE WHEN regexp_full_match({text}, '[0-9]{{8}}') "
            f"THEN try_strptime({text}, '%Y%m%d')::DATE "
            f"WHEN regexp_full_match({text}, '[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}') "
            f"THEN try_cast({text} AS DATE) ELSE NULL END")


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stored_artifact_sha256(table_root: Path) -> str:
    """Verify and return either a single-file or partitioned-dataset hash."""
    table_root = Path(table_root)
    manifest = json.loads((table_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("storage_layout") != "factor_id_partitioned_v1":
        return _file_digest(table_root / "part-00000.parquet")
    partitions = manifest.get("partitions", {})
    if not partitions:
        raise ValueError(f"Partitioned dataset has no partitions: {table_root}")
    expected_paths = {entry["path"] for entry in partitions.values()}
    actual_paths = {path.relative_to(table_root).as_posix()
                    for path in table_root.glob("factor_id=*/part-00000.parquet")}
    if actual_paths != expected_paths or (table_root / "part-00000.parquet").exists():
        raise ValueError(f"Partition file set mismatch or duplicate root file: {table_root}")
    actual = {}
    for factor_id, entry in partitions.items():
        path = table_root / entry["path"]
        digest = _file_digest(path)
        if digest != entry["sha256"]:
            raise ValueError(f"Partition hash mismatch: {table_root}/{factor_id}")
        actual[factor_id] = digest
    digest = hashlib.sha256(json_text(actual).encode()).hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError(f"Partitioned dataset hash mismatch: {table_root}")
    return digest


def factor_partition_relation(root: Path) -> str:
    pattern = Path(root) / "factor_id=*/part-00000.parquet"
    return (f"read_parquet({qs(pattern.as_posix())}, hive_partitioning=true, "
            "union_by_name=true)")


def code_identity() -> str:
    root = Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    for source_root in (root / 'src', root / 'research' / 'factors'):
        for path in sorted(source_root.rglob('*.py')):
            if source_root.name == 'src' and 'quality' in path.relative_to(source_root).parts:
                continue
            digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\0')
            digest.update(path.read_bytes())
    return digest.hexdigest()


class Store:
    def __init__(self, paths: Paths | None = None, config_root: Path | None = None, stage: str = "stage1"):
        if stage not in {"stage1", "stage2"}:
            raise ValueError(f"Unsupported store stage: {stage}")
        self.stage = stage
        self.paths = (paths or load_paths()).validate()
        self.tables = load_settings("tushare_tables.yaml", config_root)["tables"]
        self.rules = load_settings("data_middle_layer.yaml", config_root)
        self.config_hash = hashlib.sha256(json_text({"tables": self.tables, "rules": self.rules}).encode()).hexdigest()
        self.code_hash = code_identity()
        self._upstream = {}
        self.db = duckdb.connect()
        self.db.execute("SET threads=4")
        self.db.execute("SET memory_limit='2GB'")
        temp = self.output("_tmp")
        temp.mkdir(parents=True, exist_ok=True)
        self.db.execute(f"SET temp_directory={qs(temp.as_posix())}")
        self._raw = set()
        self.write_json(("_provenance", "configs", self.config_hash + ".json"),
                        {"tables": self.tables, "rules": self.rules})

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def output(self, *parts: str) -> Path:
        if self.stage == "stage2" and parts and parts[0] in {"verification", "_tmp", "_provenance"}:
            return self.paths.factor_output(*parts)
        return self.paths.output(*parts)

    def raw(self, table: str) -> str:
        if table not in self.tables:
            raise ValueError(f"Raw table is not configured: {table}")
        view = "raw_" + table
        if table in self._raw:
            return qi(view)
        spec = self.tables[table]
        fixture = self.paths.data_root / f"{table}.csv"
        if fixture.is_file():
            reader = f"read_csv({qs(fixture.as_posix())}, header=true, all_varchar=true, filename=true)"
            source_row = "row_number() OVER () - 1"
            exclude = "filename"
        else:
            pattern = self.paths.data_root / spec["path"]
            # The configured input cannot escape the raw tree.
            if not pattern.resolve().is_relative_to(self.paths.data_root):
                raise ValueError(f"Raw input path escapes DATA_ROOT: {table}")
            reader = (f"read_parquet({qs(pattern.as_posix())}, union_by_name=true, "
                      "hive_partitioning=false, filename=true, file_row_number=true)")
            source_row = "file_row_number"
            exclude = "filename, file_row_number"
        prefix = self.paths.data_root.as_posix().rstrip("/") + "/"
        normalized_filename = f"replace(filename, {qs(chr(92))}, '/')"
        self.db.execute(f"CREATE VIEW {qi(view)} AS SELECT * EXCLUDE ({exclude}), "
                        f"replace({normalized_filename}, {qs(prefix)}, '') AS _source_file, "
                        f"{source_row}::BIGINT AS _source_row FROM {reader}")
        fields = self.columns(qi(view))
        missing = sorted(set(spec.get("required_fields", [])) - set(fields))
        if missing:
            raise ValueError(f"{table}: missing configured fields: {missing}; update configuration and source notes first")
        self._raw.add(table)
        return qi(view)

    def columns(self, relation: str) -> dict:
        return {row[0]: row[1] for row in self.db.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}

    def rows(self, query: str) -> list[dict]:
        cursor = self.db.execute(query)
        names = [col[0] for col in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def write_json(self, parts: tuple[str, ...], value) -> Path:
        target = self.output(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output(*parts[:-1], parts[-1] + ".tmp")
        temporary.write_text(json_text(value), encoding="utf-8")
        os.replace(temporary, target)
        return target

    def output_relation(self, layer: str, name: str) -> str:
        root = self.output(layer, name)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        if manifest.get("storage_layout") == "factor_id_partitioned_v1":
            if not manifest.get("partitions"):
                raise FileNotFoundError(f"Partitioned upstream has no factors: {layer}/{name}")
            self._upstream[f'{layer}/{name}'] = manifest.get('sha256')
            return factor_partition_relation(root)
        path = root / "part-00000.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"Required upstream output is missing: {layer}/{name}")
        if manifest:
            self._upstream[f'{layer}/{name}'] = manifest.get('sha256')
        return f"read_parquet({qs(path.as_posix())})"

    def _publish_factor_partitions(self, layer, name, query, key, partition_by, upstream):
        import pyarrow.parquet as pq

        if partition_by not in key:
            raise ValueError('Publication partition must be a primary-key field')
        root = self.output(layer, name)
        staging = self.output(layer, f'.{name}.factor_staging')
        backup = self.output(layer, f'.{name}.factor_backup')
        field = qi(partition_by)
        order = ', '.join(qi(col) for col in key)
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists():
            shutil.rmtree(backup)
        staging.mkdir(parents=True)
        try:
            if root.exists():
                for item in root.iterdir():
                    if item.name in {'manifest.json', 'part-00000.parquet'} or item.name.startswith('factor_id='):
                        continue
                    if item.is_dir():
                        shutil.copytree(item, staging / item.name)
                    else:
                        shutil.copy2(item, staging / item.name)
            values = self.db.execute(f'SELECT DISTINCT {field} FROM ({query}) ORDER BY {field}').fetchall()
            if not values:
                raise ValueError(f'{name}: no factor partitions to publish')
            arrow_schema = self.db.execute(f'SELECT * FROM ({query}) LIMIT 0').to_arrow_table().schema
            partition_index = arrow_schema.get_field_index(partition_by)
            if partition_index < 0:
                raise ValueError(f'{name}: missing partition field {partition_by}')
            arrow_schema = arrow_schema.remove(partition_index)
            row_group_size = max(2048, min(100000, (1048576 // len(arrow_schema) // 2048) * 2048))
            partitions = {}
            for (value,) in values:
                if value is None or not re.fullmatch(r'[A-Za-z0-9_.-]+', str(value)):
                    raise ValueError(f'{name}: unsafe factor partition value {value!r}')
                predicate = f'{field}={qs(value)}'
                relative = Path(f'factor_id={value}') / 'part-00000.parquet'
                target = staging / relative
                target.parent.mkdir(parents=True)
                with pq.ParquetWriter(target, arrow_schema, compression='zstd') as writer:
                    reader = self.db.execute(f'SELECT * EXCLUDE ({field}) FROM ({query}) '
                                             f'WHERE {predicate} ORDER BY {order}').to_arrow_reader(row_group_size)
                    for batch in reader:
                        writer.write_batch(batch, row_group_size=row_group_size)
                rows = self.db.execute(f'SELECT count(*) FROM read_parquet({qs(target.as_posix())})').fetchone()[0]
                partitions[str(value)] = {'path': relative.as_posix(), 'rows': rows,
                                          'sha256': _file_digest(target)}
            relation = factor_partition_relation(staging)
            counts = self.db.execute(f'SELECT count(*),count(DISTINCT ROW({order})) FROM {relation}').fetchone()
            if counts[0] != counts[1]:
                raise ValueError(f'{name}: duplicate output key {key}')
            invalid = self.db.execute(f'SELECT count(*) FROM {relation} WHERE '
                                      + ' OR '.join(f'{qi(col)} IS NULL' for col in key)).fetchone()[0]
            if invalid:
                raise ValueError(f'{name}: {invalid} rows have null primary keys')
            partition_hashes = {factor: entry['sha256'] for factor, entry in partitions.items()}
            manifest = {'table': name, 'layer': layer, 'rows': counts[0], 'primary_key': key,
                        'config_hash': self.config_hash,
                        'sha256': hashlib.sha256(json_text(partition_hashes).encode()).hexdigest(),
                        'code_hash': self.code_hash,
                        'runtime': {'python': platform.python_version(), 'duckdb': duckdb.__version__},
                        'raw_tables': sorted(self._raw), 'upstream': upstream,
                        'schema': self.columns(relation), 'storage_layout': 'factor_id_partitioned_v1',
                        'partition_by': 'factor_id', 'partitions': partitions}
            (staging / 'manifest.json').write_text(json_text(manifest), encoding='utf-8')
            if root.exists():
                os.replace(root, backup)
            os.replace(staging, root)
            if backup.exists():
                shutil.rmtree(backup)
            self._upstream[f'{layer}/{name}'] = manifest['sha256']
            return manifest
        except Exception:
            if not root.exists() and backup.exists():
                os.replace(backup, root)
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def publish(self, layer: str, name: str, query: str, key: list[str], *, partition_by: str | None = None) -> dict:
        upstream = {k: v for k, v in self._upstream.items() if k != f'{layer}/{name}'}
        if self.stage == 'stage2' and partition_by == 'factor_id':
            return self._publish_factor_partitions(layer, name, query, key, partition_by, upstream)
        target = self.output(layer, name, "part-00000.parquet")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output(layer, name, "part-00000.tmp.parquet")
        order = ", ".join(qi(col) for col in key)
        # Bound cells per row group: a 600-column financial union cannot use the
        # same 100k-row buffers as a narrow calendar/market table.
        column_count = len(self.columns(f"({query})"))
        row_group_size = max(2048, min(100000, (1048576 // column_count // 2048) * 2048))
        if temporary.exists():
            temporary.unlink()
        if partition_by is None:
            self.db.execute(f"COPY (SELECT * FROM ({query}) ORDER BY {order}) "
                            f"TO {qs(temporary.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {row_group_size})")
        else:
            # Keep one portable Parquet artifact, but sort/stream independent
            # key groups so wide unions never require one giant sort. The
            # partition field may be any primary-key field: Stage 1 partitions
            # by the leading source_table, while factor_value partitions by
            # factor_id (the last key field) and sorts by the full key inside
            # each factor block.
            import pyarrow.parquet as pq
            if partition_by not in key:
                raise ValueError('Publication partition must be a primary-key field')
            field = qi(partition_by)
            values = self.db.execute(f'SELECT DISTINCT {field} FROM ({query}) ORDER BY {field}').fetchall()
            schema = self.db.execute(f'SELECT * FROM ({query}) LIMIT 0').to_arrow_table().schema
            with pq.ParquetWriter(temporary, schema, compression='zstd') as writer:
                for (value,) in values:
                    predicate = f'{field} IS NULL' if value is None else f'{field}={qs(value)}'
                    reader = self.db.execute(f'SELECT * FROM ({query}) WHERE {predicate} ORDER BY {order}').to_arrow_reader(row_group_size)
                    for batch in reader:
                        writer.write_batch(batch, row_group_size=row_group_size)
        rel = f"read_parquet({qs(temporary.as_posix())})"
        counts = self.db.execute(f"SELECT count(*), count(DISTINCT ROW({order})) FROM {rel}").fetchone()
        if counts[0] != counts[1]:
            samples = self.rows(f"SELECT {order}, count(*) AS duplicate_count FROM {rel} "
                                f"GROUP BY {order} HAVING count(*)>1 LIMIT 10")
            raise ValueError(f"{name}: duplicate output key {key}: {samples}")
        invalid = self.db.execute(f"SELECT count(*) FROM {rel} WHERE " + " OR ".join(f"{qi(col)} IS NULL" for col in key)).fetchone()[0]
        if invalid:
            raise ValueError(f"{name}: {invalid} rows have null primary keys {key}")
        os.replace(temporary, target)
        with target.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        manifest = {"table": name, "layer": layer, "rows": counts[0], "primary_key": key,
                    "config_hash": self.config_hash, "sha256": digest.hexdigest(),
                    "code_hash": self.code_hash, "runtime": {"python": platform.python_version(), "duckdb": duckdb.__version__},
                    "raw_tables": sorted(self._raw), "upstream": upstream,
                    "schema": self.columns(f"read_parquet({qs(target.as_posix())})")}
        self.write_json((layer, name, "manifest.json"), manifest)
        # Later outputs in the same build must reference this publication, not
        # the previous manifest that existed before the atomic replacement.
        self._upstream[f'{layer}/{name}'] = manifest['sha256']
        return manifest
