"""Append-only factor trial log and the locked-out-of-sample access gate.

The canonical history is JSONL (records are only ever appended). A Parquet
snapshot is rebuilt after each run for downstream L4 consumers. A factor
version may formally touch the out-of-sample interval at most once, across
all processes/horizons/universes; a higher factor version is a new research
object and is allowed once in turn. Evaluation code must call guard_oos_access
BEFORE computing any out-of-sample metric, so a rejected run cannot even read
the restricted labels.
"""
import json
from datetime import datetime, timezone

from core.factor.registry import factor_map


def _log_path(store):
    return store.paths.output("l4", "factor_trial_log", "trial_log.jsonl")


def read_trial_log(store) -> list[dict]:
    path = _log_path(store)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def guard_oos_access(store, factor_ids, config_root=None):
    registry = factor_map(config_root)
    history = read_trial_log(store)
    for factor_id in factor_ids:
        version = registry[factor_id]["version"]
        previous = [r for r in history if r["factor_id"] == factor_id and r.get("oos_accessed")]
        if not previous:
            continue
        max_used_version = max(r["factor_version"] for r in previous)
        if version <= max_used_version:
            raise PermissionError(
                f"Out-of-sample access denied for {factor_id} version {version}: "
                f"already formally evaluated at version {max_used_version}. "
                "Increment factor_registry.yaml version for a genuinely new factor version.")


def append_trial_log(store, records) -> dict:
    if not records:
        raise ValueError("A trial log record is required for every evaluation run")
    guard_oos_access(store, [r["factor_id"] for r in records if r.get("oos_accessed")],
                     config_root=records[0].get("config_root"))
    path = _log_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with path.open("a", encoding="utf-8") as stream:
        for sequence, record in enumerate(records):
            payload = {k: v for k, v in record.items() if k != "config_root"}
            payload.update(trial_id=f"{run_id}-{sequence:04d}", logged_at_utc=run_id)
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    return publish_trial_snapshot(store)


def publish_trial_snapshot(store) -> dict:
    history = read_trial_log(store)
    if not history:
        raise ValueError("Cannot publish an empty factor_trial_log")
    import pandas as pd
    frame = pd.DataFrame(history)
    store.db.register("trial_log_frame", frame)
    result = store.publish("l4", "factor_trial_log", "SELECT * FROM trial_log_frame", ["trial_id"])
    store.db.unregister("trial_log_frame")
    return result
