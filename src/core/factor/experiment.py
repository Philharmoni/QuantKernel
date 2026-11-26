"""Three-action factor experiments: compute, evaluate, and promote."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import pyarrow.parquet as pq
import yaml

from core.config import load_paths
from core.config.paths import PROJECT_ROOT, Paths
from core.data.store import Store, code_identity, date_sql, factor_partition_relation, json_text, qs
from core.evaluation.builder import evaluate_factors
from core.factor.plugin import factor_source_path, load_factor_compute
from core.factor.registry import REQUIRED_FIELDS, factor_map, factor_registry_text, load_factor_registry
from core.factor.report import build_factor_coverage_report
from core.preprocess.panel import build_factor_processed
from core.quality.verification import verification_metadata, verification_mode
from core.universe.custom import CUSTOM_TABLES, custom_universe_identity

EXPERIMENT_ROOT = PROJECT_ROOT / "factor_research" / "experiments"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path, value):
    Path(path).write_text(json_text(value), encoding="utf-8")


def _link_tree(source, target):
    source, target = Path(source), Path(target)
    if not source.is_dir():
        raise FileNotFoundError(f"Missing experiment upstream: {source}")
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, destination)
        except OSError:
            shutil.copy2(path, destination)


def _resolve_experiment(path):
    path = Path(path).resolve()
    if path.name == "factor_values.parquet":
        path = path.parent / "experiment.yaml"
    elif path.is_dir():
        path = path / "experiment.yaml"
    if path.name != "experiment.yaml" or not path.is_file():
        raise FileNotFoundError(f"Expected experiment.yaml or factor_values.parquet: {path}")
    return path


def load_experiment(path):
    metadata_path = _resolve_experiment(path)
    root = metadata_path.parent
    experiment = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if experiment["status"] not in {"computed", "evaluated"}:
        raise ValueError(f"Experiment is not promotable/computable: {experiment['status']}")
    values = root / "factor_values.parquet"
    if _sha(values) != manifest["factor_values"]["sha256"]:
        raise ValueError("Experiment factor_values.parquet hash mismatch")
    if _sha(root / "code" / "compute.py") != manifest["code_sha256"]:
        raise ValueError("Experiment code snapshot hash mismatch")
    return root, experiment, manifest


def _experiment_spec(module, factor_id):
    spec = getattr(module, "SPEC", None) or factor_map().get(factor_id)
    if spec is None:
        raise ValueError(f"Unregistered experiment {factor_id} must define SPEC in compute.py")
    missing = [field for field in REQUIRED_FIELDS if field not in spec]
    if missing:
        raise ValueError(f"Experiment spec missing fields: {missing}")
    if spec["factor_id"] != factor_id:
        raise ValueError("Experiment SPEC factor_id does not match source directory")
    return dict(spec)


def run_experiment(factor_id, start=None, end=None, input_end=None, params=None,
                   note=None, profile=None, experiment_root=EXPERIMENT_ROOT, source_root=None,
                   verification="standard"):
    mode = verification_mode(verification)
    official = load_paths(profile=profile)
    source_path = factor_source_path(factor_id, source_root)
    module = load_factor_compute(factor_id, source_root)
    spec = _experiment_spec(module, factor_id)
    params = params or {}
    upstream = {}
    for layer in ("l1", "l2"):
        root = official.output(layer)
        if root.exists():
            for path in sorted(root.glob("*/manifest.json")):
                if layer == "l2" and path.parent.name in CUSTOM_TABLES:
                    continue
                upstream[f"{layer}/{path.parent.name}"] = _sha(path)
    identity = {"factor_id": factor_id, "spec": spec, "params": params,
                "start": start, "end": end, "input_end": input_end,
                "verification": mode, "code_sha256": _sha(source_path),
                "core_code_hash": code_identity(), "upstream": upstream}
    experiment_id = "exp_" + hashlib.sha256(json_text(identity).encode()).hexdigest()[:16]
    target = Path(experiment_root) / factor_id / experiment_id
    staging = target.parent / ("." + experiment_id + ".staging")
    if target.exists() or staging.exists():
        raise FileExistsError(f"Experiment already exists: {target}")
    staging.mkdir(parents=True)
    try:
        config_root = staging / "snapshot" / "configs"
        config_root.mkdir(parents=True)
        for path in (PROJECT_ROOT / "configs").glob("*.yaml"):
            shutil.copy2(path, config_root / path.name)
        factors = [item for item in load_factor_registry(config_root)["factors"]
                   if item["factor_id"] != factor_id]
        factors.append(spec)
        (config_root / "factor_registry.yaml").write_text(factor_registry_text(factors), encoding="utf-8")
        (staging / "code").mkdir()
        shutil.copy2(source_path, staging / "code" / "compute.py")
        paths = Paths(official.data_root, official.middle_root, staging / "work",
                      staging / "upstream" / "strategy_research", staging / "upstream" / "verification")
        with Store(paths, config_root=config_root, stage="stage2") as store:
            relation = module.compute(store, start=start, end=end, input_end=input_end,
                                      spec=spec, params=params)
            store.db.execute(f"CREATE TEMP TABLE experiment_full AS SELECT * FROM {relation}")
            state_path = official.output("l1", "daily_stock_state", "part-00000.parquet")
            name_relation = f"read_parquet({qs(state_path.as_posix())})" if state_path.is_file() else None
            name_sql = "s.historical_name" if name_relation else "NULL::VARCHAR"
            name_join = "LEFT JOIN " + name_relation + " s USING(trade_date,ts_code)" if name_relation else ""
            output = staging / "factor_values.parquet"
            store.db.execute(f"""COPY (
                SELECT f.trade_date,f.ts_code,{name_sql} AS stock_name,f.factor_id,f.value
                FROM experiment_full f {name_join}
                ORDER BY f.trade_date,f.ts_code
            ) TO {qs(output.as_posix())} (FORMAT PARQUET,COMPRESSION ZSTD)""")
            values_rel = f"read_parquet({qs(output.as_posix())})"
            stats = store.db.execute(f"""SELECT count(*),count(DISTINCT ROW(trade_date,ts_code,factor_id)),
                min(trade_date),max(trade_date),count(DISTINCT factor_id),
                sum(CASE WHEN value IS NULL OR NOT isfinite(value) THEN 1 ELSE 0 END)
                FROM {values_rel}""").fetchone()
            if not stats[0] or stats[0] != stats[1] or stats[5]:
                raise ValueError(f"Invalid factor experiment values: {stats}")
            truncation_date = end if mode == "audit" else None
            truncation = {"status": "not_requested", "passed": None}
            if truncation_date:
                truncated = module.compute(store, start=truncation_date, end=truncation_date,
                                           input_end=truncation_date, spec=spec, params=params)
                mismatches = store.db.execute(f"""SELECT count(*) FROM (
                    (SELECT trade_date,ts_code,factor_id,value FROM experiment_full WHERE trade_date={date_sql(qs(truncation_date))}
                     EXCEPT SELECT trade_date,ts_code,factor_id,value FROM {truncated})
                    UNION ALL
                    (SELECT trade_date,ts_code,factor_id,value FROM {truncated}
                     EXCEPT SELECT trade_date,ts_code,factor_id,value FROM experiment_full WHERE trade_date={date_sql(qs(truncation_date))})
                )""").fetchone()[0]
                truncation = {"status": "executed", "date": truncation_date,
                              "mismatches": mismatches, "passed": mismatches == 0}
                if mismatches:
                    raise ValueError(f"Experiment truncation mismatch: {mismatches}")
        schema = {field.name: str(field.type) for field in pq.read_schema(output)}
        manifest = {"experiment_id": experiment_id, "factor_id": factor_id,
                    "factor_values": {"path": "factor_values.parquet", "rows": stats[0],
                                      "sha256": _sha(output), "schema": schema,
                                      "primary_key": ["trade_date", "ts_code", "factor_id"]},
                    "code_sha256": identity["code_sha256"],
                    "core_code_hash": identity["core_code_hash"],
                    "upstream": upstream, "truncation": truncation,
                    **verification_metadata(mode)}
        _write_json(staging / "manifest.json", manifest)
        experiment = {"version": 1, "experiment_id": experiment_id, "factor_id": factor_id,
                      "factor_spec": spec, "params": params, "note": note,
                      "start": start, "end": end, "input_end": input_end, "status": "computed",
                      "verification": mode, "source": f"research/factors/{factor_id}/compute.py",
                      "code_sha256": identity["code_sha256"], "upstream": upstream,
                      "rows": stats[0], "min_trade_date": str(stats[2]), "max_trade_date": str(stats[3])}
        (staging / "experiment.yaml").write_text(yaml.safe_dump(experiment, allow_unicode=True, sort_keys=False), encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
        return {"root": target, "experiment": experiment, "manifest": manifest}
    except Exception:
        if staging.exists():
            (staging / "FAILED").write_text("Incomplete experiment; do not evaluate or promote\n", encoding="utf-8")
        raise


def _peer_correlations(store, candidate, official, research):
    local = store.output_relation("l3", "factor_processed")
    peers = factor_partition_relation(official.output("l3", "factor_processed"))
    universe = qs(research["composition"]["universe_id"])
    return store.rows(f"""WITH candidate AS (
        SELECT trade_date,ts_code,value FROM {local}
        WHERE factor_id={qs(candidate)} AND universe_id={universe} AND process_id='ws_z'
    ), paired AS (
        SELECT c.trade_date,p.factor_id AS peer,c.value AS candidate_value,p.value AS peer_value
        FROM candidate c JOIN {peers} p USING(trade_date,ts_code)
        WHERE p.universe_id={universe} AND p.process_id='ws_z' AND p.factor_id<>{qs(candidate)}
    ), daily AS (
        SELECT trade_date,peer,corr(candidate_value,peer_value) AS correlation
        FROM paired GROUP BY 1,2 HAVING count(*)>=30
    ) SELECT peer,count(*) AS days,avg(correlation) AS mean_correlation,
      max(abs(correlation)) AS max_abs_daily_correlation FROM daily GROUP BY peer ORDER BY peer""")


def evaluate_experiment(path, profile=None, universe=None):
    root, experiment, compute_manifest = load_experiment(path)
    official = load_paths(profile=profile)
    candidate = experiment["factor_id"]
    config_root = root / "snapshot" / "configs"
    if universe is not None and not re.fullmatch(r"[a-z][a-z0-9_]*", universe):
        raise ValueError(f"Invalid custom universe_id: {universe!r}")
    evaluation = root / "evaluation" if universe is None else root / "evaluations" / universe
    if evaluation.exists():
        raise FileExistsError(f"Evaluation already exists: {evaluation}")
    middle = (root / "work" / "upstream_middle" if universe is None else
              root / "work" / "universes" / universe / "upstream_middle")
    paths = Paths(official.data_root, middle, evaluation)
    upstream_tables = [("l1", "daily_calendar"), ("l1", "daily_adjusted_price"),
                       ("l1", "daily_stock_state")]
    upstream_tables += ([("l2", name) for name in CUSTOM_TABLES] if universe is not None
                        else [("l2", "daily_research_universe")])
    for layer, name in upstream_tables:
        _link_tree(official.output(layer, name), paths.output(layer, name))
    for name in ("daily_industry", "daily_index_member", "forward_return"):
        _link_tree(official.output("l3", name), paths.output("l3", name))
    research = yaml.safe_load((config_root / "factor_research.yaml").read_text(encoding="utf-8"))
    selected_universe = universe or research["composition"]["universe_id"]
    market_cap_root = official.output("l3", "factor_value")
    market_cap = factor_partition_relation(market_cap_root)
    with Store(paths, config_root=config_root, stage="stage2") as store:
        store._upstream["l3/factor_value:log_circ_mv"] = _sha(market_cap_root / "manifest.json")
        values = root / "factor_values.parquet"
        store.publish("l3", "factor_value",
                      f"SELECT trade_date,ts_code,factor_id,value FROM read_parquet({qs(values.as_posix())})",
                      ["trade_date", "ts_code", "factor_id"], partition_by="factor_id")
        build_factor_coverage_report(store, config_root=config_root, factor_ids=[candidate])
        build_factor_processed(store, experiment["start"], experiment["end"], config_root,
                               [candidate], False, market_cap, custom_universe_id=universe)
        results = evaluate_factors(store, experiment["start"], experiment["end"], [candidate],
                                   config_root=config_root, merge_existing=False,
                                   market_cap_relation=market_cap, custom_universe_id=universe)
        summary_relation = store.output_relation("l4", "factor_summary")
        rows = store.rows(f"""SELECT * FROM {summary_relation}
            WHERE factor_id={qs(candidate)} AND universe_id={qs(selected_universe)}
              AND process_id='ws_z' AND sample_mode='theoretical' AND segment_id='full'
              AND period_id IN ('in_sample','full') ORDER BY label_id""")
        in_sample_rows = [row for row in rows if row["period_id"] == "in_sample"]
        if in_sample_rows:
            rows = in_sample_rows
        if not rows:
            raise ValueError("Experiment produced no primary evaluation summary")
        correlations = [] if universe is not None else _peer_correlations(store, candidate, official, research)
        coverage = json.loads(paths.output("l3", "factor_value", "coverage_report.json").read_text())
        processing = json.loads(paths.output("l3", "factor_processed", "processing_report.json").read_text())
        summary = {"factor_id": candidate, "experiment_id": experiment["experiment_id"],
                   "status": "evaluated", "technical_passed": True,
                   "query_scope": {"universe_id": selected_universe,
                                   "process_id": "ws_z", "sample_mode": "theoretical",
                                   "segment_id": "full", "period_id": "in_sample"},
                   "metrics": rows, "coverage": coverage["summary"],
                   "preprocessing": processing["summary"], "peer_correlations": correlations,
                   "table_manifests": {name: manifest["sha256"] for name, manifest in results.items()},
                   "oos_evaluated": False}
        if universe is not None:
            summary["query_scope"]["universe_type"] = "custom_file"
            summary["peer_correlations_status"] = "skipped_custom_universe"
        _write_json(evaluation / "summary.json", summary)
        lines = [f"# Factor experiment: {candidate}", "",
                 f"Experiment: `{experiment['experiment_id']}`", "",
                 "| Horizon | RankIC | RankICIR | NW t | Long/short annualized | Long/short Sharpe |",
                 "|---|---:|---:|---:|---:|---:|"]
        for row in rows:
            lines.append(f"| {row['label_id']} | {row['rank_ic_mean']:.6f} | {row['rank_icir']:.6f} "
                         f"| {row['rank_ic_t_stat']:.4f} | {row['long_short_annualized_return']:.6f} "
                         f"| {row['long_short_sharpe']:.4f} |")
        lines += ["", "Detailed metrics and peer correlations are in `summary.json`.", ""]
        (evaluation / "report.md").write_text("\n".join(lines), encoding="utf-8")
        manifest = {"factor_id": candidate, "experiment_id": experiment["experiment_id"],
                    "technical_passed": True, "compute_manifest_sha256": _sha(root / "manifest.json"),
                    "summary_sha256": _sha(evaluation / "summary.json"),
                    "report_sha256": _sha(evaluation / "report.md"),
                    "source_code_sha256": compute_manifest["code_sha256"],
                    "table_manifests": summary["table_manifests"]}
        if universe is not None:
            manifest["universe"] = custom_universe_identity(store, universe)
        _write_json(evaluation / "manifest.json", manifest)
    return summary
