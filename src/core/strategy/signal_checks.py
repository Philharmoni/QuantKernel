"""Independent Step 3 output, truncation and repeatability checks."""
import json
from pathlib import Path

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import file_sha256, parse_ymd
from core.strategy.inputs import SnapshotInputs, quote_string
from core.quality.verification import verification_mode
from core.strategy.signals import TABLE_KEYS, build_signal_views, publish_signals
from core.strategy.verification import repeat_metadata, resolve_repeat, table_repeat_mismatches


def check_signal_run(run_root, bundle_root, config, truncation_date="20200430", repeat_root=None, verification=None):
    mode = verification_mode(verification)
    run_root = Path(run_root)
    run_manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    failures = []
    observations = {}
    with SnapshotInputs(bundle_root, verification=mode) as inputs:
        db = inputs.db
        for table, key in TABLE_KEYS.items():
            entry = run_manifest["tables"][table]
            path = run_root / entry["path"]
            if mode == "audit" and file_sha256(path) != entry["sha256"]:
                failures.append({"check": "hash", "table": table})
            relation = f"read_parquet({quote_string(str(path))})"
            fields = ",".join(key)
            count, distinct = db.execute(f"SELECT count(*),count(DISTINCT ROW({fields})) FROM {relation}").fetchone()
            if count != entry["rows"] or count != distinct:
                failures.append({"check": "primary_key_or_rows", "table": table})
        signal = f"read_parquet({quote_string(str(run_root / 'l3/strategy_signal/part-00000.parquet'))})"
        universe = f"read_parquet({quote_string(str(run_root / 'l3/strategy_universe/part-00000.parquet'))})"
        daily = f"read_parquet({quote_string(str(run_root / 'l3/strategy_signal_daily/part-00000.parquet'))})"
        observations["score_formula_violations"] = db.execute(f"""SELECT count(*) FROM {signal}
            WHERE score IS NOT NULL AND abs(score-((value_module+reversal_module+low_vol_module)/3))>1e-12""").fetchone()[0]
        observations["ineligible_with_rank"] = db.execute(f"SELECT count(*) FROM {signal} WHERE NOT is_signal_eligible AND score_rank IS NOT NULL").fetchone()[0]
        observations["eligible_missing_factor"] = db.execute(f"SELECT count(*) FROM {signal} WHERE is_signal_eligible AND NOT factors_complete").fetchone()[0]
        observations["rank_sequence_violations"] = db.execute(f"""SELECT count(*) FROM (
            SELECT trade_date,count(*) AS n,min(score_rank) AS first,max(score_rank) AS last
            FROM {signal} WHERE is_signal_eligible GROUP BY trade_date
            ) WHERE n<>last OR first<>1""").fetchone()[0]
        observations["daily_gate_violations"] = db.execute(f"""SELECT count(*) FROM {daily}
            WHERE day_gate_pass AND (factor_complete_count<{int(config['universe']['min_cross_section'])}
               OR common_factor_coverage<{float(config['universe']['min_common_factor_coverage'])}
               OR min_score>=max_score)""").fetchone()[0]
        observations["eligibility_disagreement"] = db.execute(f"""SELECT count(*) FROM {signal} s JOIN {universe} u
            ON s.trade_date=u.trade_date AND s.ts_code=u.ts_code
            WHERE s.is_signal_eligible<>u.is_signal_eligible""").fetchone()[0]
        observations["oos_rows"] = db.execute(f"SELECT count(*) FROM {signal} WHERE trade_date>DATE '{parse_ymd(config['permissions']['in_sample_end']).isoformat()}'").fetchone()[0]
        if "l3/forward_return" in run_manifest["input_read_log"] or run_manifest["research_labels_read"]:
            failures.append({"check": "research_label_access"})
        for name, value in observations.items():
            if value:
                failures.append({"check": name, "count": value})
        if mode == "audit":
            cutoff = parse_ymd(truncation_date)
            build_signal_views(inputs, config, as_of=truncation_date)
            observations["truncation_date"] = cutoff.isoformat()
            observations["truncation_differences"] = {}
            for table in TABLE_KEYS:
                published = f"read_parquet({quote_string(str(run_root / ('l3/' + table + '/part-00000.parquet')) )})"
                difference = db.execute(f"""SELECT count(*) FROM (
                    (SELECT * FROM {table} EXCEPT ALL SELECT * FROM {published} WHERE trade_date<=DATE '{cutoff.isoformat()}')
                    UNION ALL
                    (SELECT * FROM {published} WHERE trade_date<=DATE '{cutoff.isoformat()}' EXCEPT ALL SELECT * FROM {table})
                )""").fetchone()[0]
                observations["truncation_differences"][table] = difference
                if difference:
                    failures.append({"check": "truncation", "table": table, "count": difference})
        else:
            observations["truncation_status"] = "not_requested"
    def repeat_publisher():
        with SnapshotInputs(bundle_root, verification=mode) as repeat_inputs:
            return publish_signals(repeat_inputs, repeat_root, config, run_id=repeat_root.name)
    repeat, repeat_status = resolve_repeat(
        repeat_root, repeat_publisher, mode) if repeat_root is not None else (None, "not_requested")
    observations["repeat_directory"] = str(repeat_root) if repeat is not None else None
    observations["repeat_hash_mismatches"] = table_repeat_mismatches(run_manifest, repeat, TABLE_KEYS)
    if observations["repeat_hash_mismatches"]:
        failures.append({"check": "repeat_hash", "tables": observations["repeat_hash_mismatches"]})
    return {"passed": not failures, "step": 3, "run_id": run_manifest["run_id"],
            "failures": failures, "observations": observations,
            "research_labels_read": False, "live_ready": False,
            **repeat_metadata(mode, repeat_status)}


def record_signal_checks(run_root, bundle_root, config, truncation_date="20200430", repeat_root=None, verification=None):
    result = check_signal_run(run_root, bundle_root, config, truncation_date, repeat_root, verification)
    target = Path(run_root) / "verification/step3_checks.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = Path(run_root) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification"] = {"passed": result["passed"], "path": "verification/step3_checks.json",
                                "sha256": file_sha256(target)}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
