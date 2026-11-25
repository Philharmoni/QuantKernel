"""Promotion of a fully evaluated experiment into the accepted factor registry."""
import hashlib
import json
import os
from pathlib import Path

import yaml

from core.config.paths import PROJECT_ROOT
from core.data.store import json_text
from core.factor.experiment import load_experiment
from core.factor.plugin import factor_source_path
from core.factor.registry import factor_registry_block


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _primary_rows(summary, horizons):
    selected = {}
    for row in summary["metrics"]:
        horizon = int(row["label_id"].rsplit("_", 1)[-1])
        if horizon in horizons and row["period_id"] == "in_sample":
            selected[horizon] = row
    for row in summary["metrics"]:
        horizon = int(row["label_id"].rsplit("_", 1)[-1])
        if horizon in horizons and horizon not in selected and row["period_id"] == "full":
            selected[horizon] = row
    return selected


def promotion_checks(root, experiment, approve=False, policy_root=PROJECT_ROOT / "configs", source_root=None):
    evaluation = Path(root) / "evaluation"
    manifest = json.loads((evaluation / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((evaluation / "summary.json").read_text(encoding="utf-8"))
    if _sha(evaluation / "summary.json") != manifest["summary_sha256"]:
        raise ValueError("Evaluation summary hash mismatch")
    if _sha(evaluation / "report.md") != manifest["report_sha256"]:
        raise ValueError("Evaluation report hash mismatch")
    current_hash = _sha(factor_source_path(experiment["factor_id"], source_root))
    policy = yaml.safe_load((Path(policy_root) / "factor_admission.yaml").read_text(encoding="utf-8"))
    rows = _primary_rows(summary, set(policy["require_primary_horizons"]))
    failures = []
    if not manifest["technical_passed"] or not summary["technical_passed"]:
        failures.append("technical_gate")
    if current_hash != experiment["code_sha256"]:
        failures.append("source_changed_after_evaluation")
    if policy["require_manual_approval"] and not approve:
        failures.append("manual_approval_required")
    missing = sorted(set(policy["require_primary_horizons"]) - set(rows))
    if missing:
        failures.append(f"missing_primary_horizons:{missing}")
    for horizon, row in rows.items():
        if (row["rank_ic_n_days"] or 0) < policy["min_rank_ic_days"]:
            failures.append(f"rank_ic_days:h{horizon}")
        if policy["min_abs_rank_ic"] is not None and abs(row["rank_ic_mean"]) < policy["min_abs_rank_ic"]:
            failures.append(f"abs_rank_ic:h{horizon}")
        if policy["min_abs_rank_icir"] is not None and abs(row["rank_icir"]) < policy["min_abs_rank_icir"]:
            failures.append(f"abs_rank_icir:h{horizon}")
    if policy["max_abs_peer_correlation"] is not None:
        for row in summary.get("peer_correlations", []):
            if row["mean_correlation"] is not None and abs(row["mean_correlation"]) > policy["max_abs_peer_correlation"]:
                failures.append(f"peer_correlation:{row['peer']}")
    return {"passed": not failures, "failures": sorted(failures),
            "manual_approved": approve, "source_code_sha256": current_hash,
            "evaluation_manifest_sha256": _sha(evaluation / "manifest.json"),
            "primary_metrics": rows, "policy": policy}

def promote_experiment(path, approve=False, registry_path=PROJECT_ROOT / "configs" / "factor_registry.yaml", source_root=None):
    root, experiment, _ = load_experiment(path)
    registry_path = Path(registry_path)
    checks = promotion_checks(root, experiment, approve=approve,
                              policy_root=registry_path.parent, source_root=source_root)
    factor_id = experiment["factor_id"]
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or "factors" in registry:
        raise ValueError("Factor registry must use top-level factor_id mappings")
    if factor_id in registry:
        raise ValueError(f"Factor already exists in library; explicit version migration required: {factor_id}")
    result = {"factor_id": factor_id, "experiment_id": experiment["experiment_id"],
              "checks": checks, "registry": str(registry_path), "promoted": False}
    if not checks["passed"]:
        _write_promotion_record(root, result)
        raise ValueError(f"Factor promotion blocked: {checks['failures']}")
    original = registry_path.read_text(encoding="utf-8")
    block = factor_registry_block(experiment["factor_spec"])
    updated = original.rstrip() + "\n\n" + block + "\n"
    lock = registry_path.with_suffix(registry_path.suffix + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        temporary = registry_path.with_suffix(registry_path.suffix + ".tmp")
        temporary.write_text(updated, encoding="utf-8")
        parsed = yaml.safe_load(temporary.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or factor_id not in parsed or "factor_id" in parsed[factor_id]:
            raise ValueError("Registry update validation failed")
        os.replace(temporary, registry_path)
    finally:
        lock.unlink(missing_ok=True)
    result["promoted"] = True
    result["next"] = "Run build_stage2.py to rebuild accepted artifacts; prepare a new Stage 3 snapshot if required"
    _write_promotion_record(root, result)
    return result


def _write_promotion_record(root, result):
    (Path(root) / "promotion.json").write_text(json_text(result), encoding="utf-8")
