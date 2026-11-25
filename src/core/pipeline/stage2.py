"""Canonical, fail-fast Stage 2 rebuild from Stage 1 artifacts through Step 10.

The orchestrator does not download data or touch out-of-sample evaluation. It
also creates the bounded independent-run evidence required by the final quality
gate, making a fresh-device run self-contained rather than dependent on old
verification directories.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from core.benchmark.calibration import build_benchmark_calibration
from core.benchmark.membership import build_daily_index_member
from core.benchmark.returns import build_daily_benchmark_return
from core.config import Paths, load_paths
from core.config.paths import PROJECT_ROOT
from core.config.settings import load_settings
from core.data.inventory import inventory
from core.data.store import Store, json_text
from core.evaluation.builder import evaluate_factors
from core.evaluation.composite import build_composite_factors
from core.evaluation.correlation import build_factor_correlation
from core.evaluation.report import build_factor_reports
from core.factor.baseline import build_factor_values
from core.factor.entry import check_stage2_entry
from core.factor.registry import factor_map
from core.factor.report import build_factor_coverage_report
from core.factor.truncate import check_truncation_invariance
from core.industry.pit import build_daily_industry
from core.label.forward import build_forward_return
from core.preprocess.panel import build_factor_processed
from core.quality.stage2 import REPEAT_SPECS, REPEAT_STEP9_ROOTS, run_stage2_quality_gate
from core.quality.verification import verification_metadata, verification_mode
from core.reference.market import build_index_daily_available
from core.reference.membership import build_index_member_available, build_index_weight_available


PIPELINE_STEPS = [
    "step00_stage1_admission", "step01_golden_and_unit_tests", "step02_reference_and_membership",
    "step03_forward_labels", "step04_benchmark_and_calibration", "step05_baseline_factors",
    "step06_preprocessing", "step07_single_factor_evaluation",
    "step08_correlation_and_composites", "step09_reports",
    "step10_raw_unchanged_and_unified_quality",
]


def pipeline_steps(verification="standard"):
    steps = list(PIPELINE_STEPS)
    if verification_mode(verification) == "audit":
        for anchor, repeat in (("step09_reports", "repeat_step9"),
                               ("step08_correlation_and_composites", "repeat_step8"),
                               ("step07_single_factor_evaluation", "repeat_step7")):
            steps.insert(steps.index(anchor) + 1, repeat)
    return steps
_STEP7_REQUIREMENTS = [
    ("l1", "daily_calendar", ()), ("l3", "factor_processed", ()),
    ("l3", "factor_value", ()), ("l3", "forward_return", ()), ("l3", "daily_index_member", ()),
]
_STEP8_REQUIREMENTS = [
    ("l1", "daily_calendar", ()), ("l2", "daily_research_universe", ()),
    ("l3", "factor_processed", ()), ("l3", "factor_value", ()),
    ("l3", "forward_return", ()), ("l3", "daily_industry", ()),
]
_STEP9_REQUIREMENTS = [
    ("l3", "factor_value", ("coverage_report.json",)),
    ("l3", "factor_processed", ("processing_report.json",)),
    ("l4", "factor_ic_daily", ()), ("l4", "factor_quantile_return", ()),
    ("l4", "factor_summary", ()), ("l4", "factor_correlation", ()),
    ("l4", "factor_trial_log", ("trial_log.jsonl",)),
]


def _metadata_signature(manifest):
    rows = sorted((row["path"], row["size"], row["mtime_ns"]) for row in manifest["files"])
    return {"signature": hashlib.sha256(json_text(
        {"directories": manifest["directories"], "files": rows}).encode("utf-8")).hexdigest(),
        "file_count": len(rows), "total_bytes": sum(row[1] for row in rows)}


def _link_or_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _prepare_repeat_paths(paths, root_name, requirements):
    workspace = paths.factor_root / root_name
    repeat = Paths(paths.data_root, workspace / "data_middle", workspace / "factor_research",
                   workspace / "strategy_research", workspace / "verification")
    for layer, name, extras in requirements:
        source_manifest_path = paths.output(layer, name, "manifest.json")
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        data_files = ["part-00000.parquet"]
        if source_manifest.get("storage_layout") == "factor_id_partitioned_v1":
            data_files = [entry["path"] for entry in source_manifest["partitions"].values()]
        for filename in (*data_files, "manifest.json", *extras):
            source = paths.output(layer, name, filename)
            if not source.is_file():
                raise FileNotFoundError(f"Repeat input is missing: {source}")
            target = repeat.output(layer, name, filename)
            _link_or_copy(source, target)
    return repeat


def _compare_manifest_pair(first, second, artifacts):
    comparisons = {}
    for layer, name in artifacts:
        manifests = [json.loads(root.joinpath(layer, name, "manifest.json").read_text(encoding="utf-8"))
                     for root in (first, second)]
        same = (manifests[0]["rows"] == manifests[1]["rows"]
                and manifests[0]["sha256"] == manifests[1]["sha256"])
        comparisons[f"{layer}/{name}"] = {"rows": manifests[0]["rows"], "identical": same}
        if not same:
            raise ValueError(f"Repeatability mismatch for {layer}/{name}")
    return comparisons


def _repeat_step7(paths, config_root, baseline_ids, start, end):
    roots, artifacts = REPEAT_SPECS["step7"]
    repeat_paths = []
    for root in roots:
        repeat = _prepare_repeat_paths(paths, root, _STEP7_REQUIREMENTS)
        repeat_paths.append(repeat.factor_root)
        with Store(repeat, config_root=config_root, stage="stage2") as store:
            evaluate_factors(store, start=start, end=end, factor_ids=baseline_ids, config_root=config_root)
    return _compare_manifest_pair(*repeat_paths, artifacts)


def _repeat_step8(paths, config_root, start, end, composite_ids):
    roots, artifacts = REPEAT_SPECS["step8"]
    repeat_paths = []
    for root in roots:
        repeat = _prepare_repeat_paths(paths, root, _STEP8_REQUIREMENTS)
        repeat_paths.append(repeat.factor_root)
        with Store(repeat, config_root=config_root, stage="stage2") as store:
            build_factor_correlation(store, start, end, config_root)
            build_composite_factors(store, start, end, config_root=config_root)
            build_factor_processed(store, start, end, config_root, composite_ids, merge_existing=True)
    return _compare_manifest_pair(*repeat_paths, artifacts)


def _repeat_step9(paths, config_root):
    repeat_paths = []
    for root in REPEAT_STEP9_ROOTS:
        repeat = _prepare_repeat_paths(paths, root, _STEP9_REQUIREMENTS)
        repeat_paths.append(repeat.factor_root / "l4" / "factor_report")
        with Store(repeat, config_root=config_root, stage="stage2") as store:
            build_factor_reports(store, config_root=config_root)
    manifests = [json.loads((root / "manifest.json").read_text(encoding="utf-8")) for root in repeat_paths]
    if manifests[0] != manifests[1]:
        raise ValueError("Repeatability mismatch for factor_report manifest")
    for filename, digest in manifests[0]["files"].items():
        for root in repeat_paths:
            actual = hashlib.sha256((root / filename).read_bytes()).hexdigest()
            if actual != digest:
                raise ValueError(f"Repeatability mismatch for factor_report/{filename}")
    return {"files": len(manifests[0]["files"]), "identical": True}


def _write_status(paths, config_root, payload):
    with Store(paths, config_root=config_root, stage="stage2") as store:
        store.write_json(("verification", "stage2_build_status.json"), payload)


def run_stage2_pipeline(profile=None, record_run="latest", dry_run=False,
                        deep_primary_keys=False, config_root=None, verification="standard") -> dict:
    mode = verification_mode(verification)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", record_run):
        raise ValueError("record_run accepts only letters, digits, underscore and hyphen")
    if dry_run:
        return {"dry_run": True, "steps": pipeline_steps(mode),
                "requires": ["raw DATA_ROOT", "passed Stage 1 artifacts", "Python test dependencies"],
                "network_downloads": False, "out_of_sample_evaluation": False,
                **verification_metadata(mode)}
    started = time.perf_counter()
    paths = load_paths(profile=profile)
    research = load_settings("factor_research.yaml", config_root)
    registry = factor_map(config_root)
    baseline_ids = [factor_id for factor_id, spec in registry.items() if spec["category"] != "composite"]
    composite_ids = [factor_id for factor_id, spec in registry.items() if spec["category"] == "composite"]
    truncation_dates = research["quality_gate"]["truncation_dates"]
    repeat_start = research["quality_gate"]["bounded_repeat_start"]
    repeat_end = research["quality_gate"]["bounded_repeat_end"]
    logging.info("Checking raw source metadata before Stage 2")
    before = _metadata_signature(inventory(paths.data_root))
    stages, current = [], "initialization"

    def execute(name, action, use_store=True):
        nonlocal current
        current = name
        logging.info("Starting %s", name)
        step_started = time.perf_counter()
        if use_store:
            with Store(paths, config_root=config_root, stage="stage2") as store:
                result = action(store)
        else:
            result = action()
        stages.append({"stage": name, "passed": True, "elapsed_seconds": round(time.perf_counter() - step_started, 3)})
        _write_status(paths, config_root, {"status": "running", "run": record_run, "stages": stages})
        logging.info("Passed %s", name)
        return result

    try:
        _write_status(paths, config_root, {"status": "running", "run": record_run, "stages": stages})
        with Store(paths, config_root=config_root, stage="stage2") as store:
            store.write_json(("verification", "stage2_quality_gate.json"),
                             {"passed": False, "status": "rebuilding", "run": record_run})
            store.output("verification", "stage2_quality_gate.md").write_text(
                "# Stage 2 数据处理中\n\n当前重建尚未通过统一门禁。\n", encoding="utf-8")
        entry_result = execute("step00_stage1_admission", lambda store: check_stage2_entry(
            store, config_root, verification=mode))

        def tests():
            subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=PROJECT_ROOT, check=True)
            return {"command": "python -m pytest -q"}
        execute("step01_golden_and_unit_tests", tests, use_store=False)

        def references(store):
            build_index_member_available(store)
            build_index_weight_available(store)
            build_index_daily_available(store)
            build_daily_industry(store, config_root=config_root)
            return build_daily_index_member(store, config_root=config_root)
        execute("step02_reference_and_membership", references)
        execute("step03_forward_labels", lambda store: build_forward_return(store, config_root=config_root))

        def benchmarks(store):
            built = build_daily_benchmark_return(store, config_root=config_root)
            build_benchmark_calibration(store, config_root)
            return built
        execute("step04_benchmark_and_calibration", benchmarks)

        def baseline(store):
            values = build_factor_values(store, config_root=config_root, factor_ids=baseline_ids)
            build_factor_coverage_report(store, config_root)
            gate = check_truncation_invariance(store, truncation_dates, baseline_ids, config_root)
            store.write_json(("verification", "step5_truncation.json"), gate)
            if not gate["passed"]:
                raise ValueError(f"Step 5 truncation failed: {gate['mismatches'][:5]}")
            return values
        execute("step05_baseline_factors", baseline)
        execute("step06_preprocessing", lambda store: build_factor_processed(
            store, config_root=config_root, factor_ids=baseline_ids))
        execute("step07_single_factor_evaluation", lambda store: evaluate_factors(
            store, factor_ids=baseline_ids, config_root=config_root))
        if mode == "audit":
            execute("repeat_step7", lambda store: _repeat_step7(
                store.paths, config_root, baseline_ids, repeat_start, repeat_end))

        def composites(store):
            build_composite_factors(store, config_root=config_root)
            build_factor_processed(store, config_root=config_root,
                                   factor_ids=composite_ids, merge_existing=True)
            gate = check_truncation_invariance(store, truncation_dates, composite_ids, config_root)
            store.write_json(("verification", "step8_truncation.json"), gate)
            if not gate["passed"]:
                raise ValueError(f"Step 8 truncation failed: {gate['mismatches'][:5]}")
            evaluate_factors(store, factor_ids=composite_ids, config_root=config_root, merge_existing=True)
            # Rebind non-incremental artifacts to the now-merged processed panel.
            # Values are unchanged, but their upstream hashes become current.
            values = build_composite_factors(store, config_root=config_root)
            correlation = build_factor_correlation(store, config_root=config_root)
            return {"correlation": correlation, "factor_value": values["factor_value"]}
        execute("step08_correlation_and_composites", composites)
        if mode == "audit":
            execute("repeat_step8", lambda store: _repeat_step8(
                store.paths, config_root, repeat_start, repeat_end, composite_ids))

        def reports(store):
            build_factor_coverage_report(store, config_root)
            return build_factor_reports(store, config_root=config_root)
        execute("step09_reports", reports)
        if mode == "audit":
            execute("repeat_step9", lambda store: _repeat_step9(store.paths, config_root))

        def final_gate():
            after = _metadata_signature(inventory(paths.data_root))
            proof = {"passed": before == after, "before": before, "after": after,
                     "method": "relative path, file size and mtime_ns; raw files only read"}
            _write_status(paths, config_root, {"status": "running", "run": record_run,
                                               "stages": stages, "raw_proof": proof})
            with Store(paths, config_root=config_root, stage="stage2") as store:
                store.write_json(("verification", "stage2_raw_unchanged.json"), proof)
            if not proof["passed"]:
                raise ValueError("Raw source metadata changed during Stage 2")
            with Store(paths, config_root=config_root, stage="stage2") as store:
                report = run_stage2_quality_gate(
                    store, truncation_dates=truncation_dates,
                    deep_primary_keys=deep_primary_keys, config_root=config_root,
                    verification=mode, entry_result=None if mode == "audit" else entry_result)
            if not report["passed"]:
                raise ValueError(f"Unified Stage 2 quality gate failed: {report['failures'][:5]}")
            return report
        execute("step10_raw_unchanged_and_unified_quality", final_gate, use_store=False)
        run = {"passed": True, "status": "passed", "run": record_run,
               "elapsed_seconds": round(time.perf_counter() - started, 3), "stages": stages,
               "raw_signature": before, "out_of_sample_evaluation": False,
               **verification_metadata(mode)}
        _write_status(paths, config_root, run)
        with Store(paths, config_root=config_root, stage="stage2") as store:
            store.write_json(("verification", "runs", f"stage2_{record_run}.json"), run)
        return run
    except Exception as error:
        _write_status(paths, config_root, {"status": "failed", "run": record_run,
                                           "stage": current, "error": str(error), "stages": stages})
        raise
