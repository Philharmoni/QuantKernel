"""File manifests do not decode or rewrite source files."""
import hashlib
import os
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor


KEY_TABLES = ("stock_basic", "trade_cal", "suspend_d", "limit_list_d", "stk_factor_pro",
              "income_vip", "balancesheet_vip", "cashflow_vip", "fina_indicator")


def raw_files(root: Path):
    """Fail on incomplete scans instead of silently omitting inaccessible directories."""
    def fail(error):
        raise error

    for directory, child_directories, names in os.walk(root, onerror=fail, followlinks=False):
        for name in child_directories:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"Raw directory symlink is unsupported: {path}")
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"Raw file symlink is unsupported: {path}")
            yield path


def inventory(root: Path, checksums: bool = False, workers: int = 16) -> dict:
    def inspect(path):
        stat = path.stat()
        row = {"path": path.relative_to(root).as_posix(), "size": stat.st_size,
               "mtime_ns": stat.st_mtime_ns}
        if checksums:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            row["sha256"] = digest.hexdigest()
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"Raw file changed during verification: {path}")
        return row

    paths = sorted(raw_files(root))
    if checksums:
        # map preserves path order even when disk reads finish out of order.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            files = list(executor.map(inspect, paths))
    else:
        files = [inspect(path) for path in paths]
    return {"files": files, "file_count": len(files),
            "total_bytes": sum(row["size"] for row in files),
            "directories": sorted(p.name for p in root.iterdir() if p.is_dir())}


def check_key_files(root: Path) -> dict:
    import pyarrow.parquet as pq

    result = {}
    for table in KEY_TABLES:
        if not (root / table).is_dir():
            raise FileNotFoundError(f"Required table directory is missing: {table}")
        files = sorted(p for p in raw_files(root / table) if p.suffix == ".parquet")
        if not files:
            raise FileNotFoundError(f"Required table has no Parquet files: {table}")
        # Fully decode one partition per required table, preserving all field names.
        path = files[0]
        data = pq.ParquetFile(path).read()
        result[table] = {"file_count": len(files), "sample_file": path.relative_to(root).as_posix(),
                         "sample_rows": data.num_rows, "fields": data.column_names}
    return result


def compare_inventories(local: dict, server: dict, require_checksums: bool = True) -> dict:
    def index(manifest):
        result = {}
        size = 0
        for row in manifest["files"]:
            name = row.get("path", row.get("relative_path"))
            if not isinstance(name, str) or not name or name in result:
                raise ValueError(f"Manifest contains invalid or duplicate file path: {name}")
            file_size = row.get("size", row.get("size_bytes"))
            if not isinstance(file_size, int) or file_size < 0:
                raise ValueError(f"Manifest contains invalid size: {name}")
            size += file_size
            result[name] = row
        if manifest["file_count"] != len(result) or manifest["total_bytes"] != size:
            raise ValueError("Manifest file_count/total_bytes do not match its file records")
        return result

    left, right = index(local), index(server)
    missing, extra = sorted(right.keys() - left.keys()), sorted(left.keys() - right.keys())
    size_mismatches, hash_mismatches = [], []
    hashes_checked = 0
    missing_checksums = []
    for name in sorted(left.keys() & right.keys()):
        a, b = left[name], right[name]
        if a.get("size", a.get("size_bytes")) != b.get("size", b.get("size_bytes")):
            size_mismatches.append(name)
        if all(re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")) for row in (a, b)):
            hashes_checked += 1
            if a["sha256"] != b["sha256"]:
                hash_mismatches.append(name)
        else:
            missing_checksums.append(name)
    directories_match = local["directories"] == server["directories"]
    return {"passed": not (missing or extra or size_mismatches or hash_mismatches
                            or (require_checksums and missing_checksums)) and directories_match,
            "directories_match": directories_match, "missing_local": missing, "extra_local": extra,
            "size_mismatches": size_mismatches, "hash_mismatches": hash_mismatches,
            "hashes_checked": hashes_checked, "missing_checksums": missing_checksums,
            "local_bytes": local["total_bytes"],
            "server_bytes": server["total_bytes"], "local_files": local["file_count"],
            "server_files": server["file_count"]}
