"""Read-only validation of a physical Stage 3 input bundle."""
import json
from pathlib import Path

from core.config.paths import PROJECT_ROOT
from core.factor.registry import factor_map
from core.strategy.contract import file_sha256, parse_ymd
from core.strategy.inputs import SnapshotInputs, quote_name, load_input_spec


def sql_date(field):
    return f"NULLIF(CAST({quote_name(field)} AS VARCHAR),'')::DATE"


def sql_present(field):
    return f"NULLIF(CAST({quote_name(field)} AS VARCHAR),'') IS NOT NULL"


def check_snapshot(bundle_root, project_root=PROJECT_ROOT, spec=None, registry_root=None):
    failures = []
    observations = {}
    spec = spec or load_input_spec(project_root)
    registry_root = registry_root or project_root / "configs"
    try:
        inputs = SnapshotInputs(bundle_root, project_root)
    except (OSError, ValueError, PermissionError) as error:
        return {"passed": False, "failures": [{"check": "input_access", "table": "bundle", "detail": str(error)}],
                "observations": observations, "units": spec["units"], "capabilities": spec["capabilities"], "live_ready": False}
    with inputs:
        cutoff = parse_ymd(inputs.cutoff)
        cutoff_sql = cutoff.isoformat()
        if cutoff != parse_ymd(spec["cutoff"]):
            failures.append({"check": "cutoff", "detail": inputs.cutoff})
        for artifact, rules in spec["source_tables"].items():
            try:
                relation = inputs.relation(artifact, "research_only" if artifact == "l3/forward_return" else "signal_inputs")
                key = rules["key"]
                fields = ",".join(quote_name(field) for field in key)
                count, distinct = inputs.db.execute(f"SELECT count(*),count(DISTINCT ROW({fields})) FROM {relation}").fetchone()
                if count != distinct or count != inputs.manifest["tables"][artifact]["rows"]:
                    failures.append({"check": "key_or_row_count", "table": artifact})
                nulls = inputs.db.execute(f"SELECT count(*) FROM {relation} WHERE {' OR '.join(f'{quote_name(field)} IS NULL' for field in key)}").fetchone()[0]
                if nulls:
                    failures.append({"check": "null_primary_key", "table": artifact, "count": nulls})
                for field in rules["dates"]:
                    condition = f"{sql_date(field)}>DATE '{cutoff_sql}'"
                    count = inputs.db.execute(f"SELECT count(*) FROM {relation} WHERE {condition}").fetchone()[0]
                    if count:
                        failures.append({"check": "date_beyond_cutoff", "table": artifact, "field": field, "count": count})
            except Exception as error:
                failures.append({"check": "input_access", "table": artifact, "detail": str(error)})
        if failures:
            return {"passed": False, "failures": failures, "observations": observations}
        calendar = inputs.relation("l1/daily_calendar")
        observations["calendar_rows"] = inputs.db.execute(f"SELECT count(*) FROM {calendar}").fetchone()[0]
        observations["next_date_beyond_cutoff"] = inputs.db.execute(f"SELECT count(*) FROM {calendar} WHERE {sql_date('next_trade_date')}>DATE '{cutoff_sql}'").fetchone()[0]
        financial = inputs.relation("l1/financial_available")
        observations["financial_effective_after_available"] = inputs.db.execute(f"SELECT count(*) FROM {financial} WHERE {sql_date('effective_ann_date')}>{sql_date('available_date')}").fetchone()[0]
        labels = inputs.relation("l3/forward_return", "research_only")
        observations["labels_beyond_cutoff"] = inputs.db.execute(f"SELECT count(*) FROM {labels} WHERE {sql_date('trade_date')}>DATE '{cutoff_sql}' OR {sql_date('entry_date')}>DATE '{cutoff_sql}' OR {sql_date('exit_date')}>DATE '{cutoff_sql}'").fetchone()[0]
        observations["non_null_label_missing_actual_dates"] = inputs.db.execute(f"SELECT count(*) FROM {labels} WHERE {sql_present('ret')} AND ({sql_date('entry_date')} IS NULL OR {sql_date('exit_date')} IS NULL)").fetchone()[0]
        observations["quarantined_cross_cutoff_labels"] = inputs.db.execute(f"SELECT count(*) FROM {labels} WHERE input_scope_status='quarantined'").fetchone()[0]
        observations["quarantine_retained_values"] = inputs.db.execute(f"SELECT count(*) FROM {labels} WHERE input_scope_status='quarantined' AND ({sql_present('ret')} OR {sql_date('entry_date')} IS NOT NULL OR {sql_date('exit_date')} IS NOT NULL OR {sql_present('entry_price')} OR {sql_present('exit_price')})").fetchone()[0]
        weights = inputs.relation("l4/factor_composite_weight")
        observations["weights_not_realized"] = inputs.db.execute(f"SELECT count(*) FROM {weights} WHERE {sql_date('last_exit_date')}>={sql_date('trade_date')}").fetchone()[0]
        values = inputs.relation("l3/factor_value")
        processed = inputs.relation("l3/factor_processed")
        expected_factors = set(factor_map(registry_root))
        observations["factor_value_ids"] = inputs.db.execute(f"SELECT count(DISTINCT factor_id) FROM {values}").fetchone()[0]
        observations["factor_processed_ids"] = inputs.db.execute(f"SELECT count(DISTINCT factor_id) FROM {processed}").fetchone()[0]
        actual_values = {row[0] for row in inputs.db.execute(f"SELECT DISTINCT factor_id FROM {values}").fetchall()}
        actual_processed = {row[0] for row in inputs.db.execute(f"SELECT DISTINCT factor_id FROM {processed}").fetchall()}
        if actual_values != expected_factors or actual_processed != expected_factors:
            failures.append({"check": "factor_registry_coverage", "expected": sorted(expected_factors),
                             "missing_values": sorted(expected_factors - actual_values), "missing_processed": sorted(expected_factors - actual_processed)})
        for name in ("calendar_rows",):
            if not observations[name]:
                failures.append({"check": "empty_input", "field": name})
        for name, value in observations.items():
            if name not in ("calendar_rows", "factor_value_ids", "factor_processed_ids", "quarantined_cross_cutoff_labels") and value:
                failures.append({"check": name, "count": value})
    return {"passed": not failures, "failures": failures, "observations": observations,
            "units": spec["units"], "capabilities": spec["capabilities"], "live_ready": False}


def record_snapshot_admission(bundle_root, project_root=PROJECT_ROOT):
    root = Path(bundle_root)
    result = check_snapshot(root, project_root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = dict(result, bundle_id=manifest["bundle_id"], cutoff=manifest["cutoff"],
                   table_count=len(manifest["tables"]), deep_primary_keys=result["passed"],
                   all_dates_bounded=result["passed"], raw_tables_read=[],
                   oos_performance_generated=False, real_share_account_ready=False)
    admission_path = root / "admission.json"
    admission_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
    manifest["status"] = "complete" if result["passed"] else "admission_failed"
    manifest["admission"] = {"passed": result["passed"], "path": "admission.json", "sha256": file_sha256(admission_path)}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
    return result
