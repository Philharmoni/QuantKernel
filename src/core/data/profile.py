"""Full scans: report source anomalies without changing their meaning."""
from core.data.store import Store, date_sql, qi, qs
import json


def profile_table(store: Store, table: str) -> dict:
    relation = store.raw(table)
    spec = store.tables[table]
    fields = {name: dtype for name, dtype in store.columns(relation).items() if not name.startswith("_source_")}
    dates = spec.get("date_fields", [])
    key = spec.get("primary_key", [])
    configured = set(dates + key + ([spec["code_field"]] if spec.get("code_field") else []))
    missing = sorted(configured - set(fields))
    if missing:
        raise ValueError(f"{table}: configured fields not found: {missing}")
    expressions = ["count(*) AS rows"]
    for i, field in enumerate(fields):
        expressions.append(f"count(*) FILTER (WHERE {qi(field)} IS NULL OR trim(cast({qi(field)} AS VARCHAR))='') AS null_{i}")
    for i, field in enumerate(dates):
        parsed = date_sql(qi(field))
        expressions.extend([f"min({parsed}) AS min_{i}", f"max({parsed}) AS max_{i}",
                            f"count(*) FILTER (WHERE {qi(field)} IS NOT NULL AND trim(cast({qi(field)} AS VARCHAR))<>'' AND {parsed} IS NULL) AS invalid_{i}"])
    if spec.get("code_field"):
        expressions.append(f"count(DISTINCT {qi(spec['code_field'])}) AS stock_count")
    if key:
        expressions.append("count(*) - count(DISTINCT ROW(" + ",".join(qi(c) for c in key) + ")) AS duplicate_rows")
    values = store.rows("SELECT " + ", ".join(expressions) + f" FROM {relation}")[0]
    report = {"table": table, "rows": values["rows"], "primary_key": key,
              "stock_count": values.get("stock_count"), "duplicate_rows": values.get("duplicate_rows"),
              "columns": {field: {"type": dtype, "null_count": values[f"null_{i}"],
                                     "null_rate": values[f"null_{i}"] / values["rows"] if values["rows"] else None}
                          for i, (field, dtype) in enumerate(fields.items())},
              "dates": {field: {"min": values[f"min_{i}"], "max": values[f"max_{i}"], "invalid_count": values[f"invalid_{i}"]}
                        for i, field in enumerate(dates)}, "issues": [], "config_hash": store.config_hash}
    if values.get("duplicate_rows"):
        names = ", ".join(qi(c) for c in key)
        report["issues"].append({"kind": "duplicate_key", "fields": key,
                                 "samples": store.rows(f"SELECT {names}, count(*) AS n FROM {relation} GROUP BY {names} HAVING count(*)>1 ORDER BY {names} LIMIT 10")})
    for i, field in enumerate(dates):
        if values[f"invalid_{i}"]:
            report["issues"].append({"kind": "invalid_date", "field": field,
                "samples": store.rows(f"SELECT * FROM {relation} WHERE {qi(field)} IS NOT NULL AND trim(cast({qi(field)} AS VARCHAR))<>'' AND {date_sql(qi(field))} IS NULL ORDER BY _source_file, _source_row LIMIT 5")})
    return report


def profile_all(store: Store, tables: list[str] | None = None) -> dict:
    if tables is None:
        actual = {p.name for p in store.paths.data_root.iterdir() if p.is_dir() and not p.name.startswith("_")}
        unconfigured = actual - set(store.tables)
        if unconfigured:
            raise ValueError(f"Unconfigured raw directories; update config and source notes: {sorted(unconfigured)}")
    selected = tables or sorted(store.tables)
    reports, errors = {}, {}
    for table in selected:
        try:
            report = profile_table(store, table)
            store.write_json(("l0", "profiles", table + ".json"), report)
            reports[table] = {k: report[k] for k in ("rows", "duplicate_rows", "dates")}
            print(f"Profiled {table}: {report['rows']} rows", flush=True)
        except Exception as exc:
            errors[table] = str(exc)
            print(f"FAILED {table}: {exc}", flush=True)
    summary = {"scope": "full" if tables is None else "selected", "tables": reports, "errors": errors,
               "config_hash": store.config_hash, "passed": not errors}
    store.write_json(("l0", "profile_summary.json"), summary)
    if errors:
        raise ValueError(f"Source profiling failed; see l0/profile_summary.json: {errors}")
    return summary


def verify_profiles(store: Store) -> dict:
    root = store.paths.output("l0")
    summary = json.loads((root / "profile_summary.json").read_text(encoding="utf-8"))
    if summary["scope"] != "full" or not summary["passed"] or set(summary["tables"]) != set(store.tables):
        raise ValueError("L0 requires a successful full scan of every configured source table")
    issues = []
    lines = ["# L0 全量原始数据画像", "", "原始异常只报告，不改写源数据。辅助表未确认主键的重复数显示为未评估。", "",
             "| 原始表 | 行数 | 候选键重复行 | 无效日期数 |", "|---|---:|---:|---:|"]
    prefix = store.paths.data_root.as_posix().rstrip("/") + "/"

    def normalize(value):
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            result = {key: normalize(item) for key, item in value.items()}
            if "_source_file" in result:
                result["_source_file"] = result["_source_file"].replace("\\", "/").removeprefix(prefix)
            return result
        return value

    for table, spec in sorted(store.tables.items()):
        path = root / "profiles" / (table + ".json")
        original = json.loads(path.read_text(encoding="utf-8"))
        report = normalize(original)
        if report != original:
            store.write_json(("l0", "profiles", table + ".json"), report)
        if report["primary_key"] != spec.get("primary_key", []) or set(report["dates"]) != set(spec.get("date_fields", [])):
            raise ValueError(f"{table}: processing field configuration changed; rerun full profiling")
        missing = set(spec.get("required_fields", [])) - set(report["columns"])
        if missing:
            raise ValueError(f"{table}: missing configured columns: {sorted(missing)}")
        archived = store.paths.output("_provenance", "configs", report["config_hash"] + ".json")
        if not archived.is_file():
            raise ValueError(f"{table}: configuration provenance is missing: {report['config_hash']}")
        invalid = sum(item["invalid_count"] for item in report["dates"].values())
        duplicate = report["duplicate_rows"] if report["duplicate_rows"] is not None else "未评估"
        lines.append(f"| {table} | {report['rows']:,} | {duplicate} | {invalid} |")
        issues.extend({"table": table, **issue} for issue in report["issues"])
    result = {"passed": True, "table_count": len(store.tables),
              "core_table_count": sum(bool(spec.get("stage1")) for spec in store.tables.values()),
              "issues": issues, "config_hash": store.config_hash}
    store.write_json(("l0", "verification.json"), result)
    target = store.paths.output("l0", "DATA_PROFILE.md")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
