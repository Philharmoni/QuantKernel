"""Write the Step 8 pre-registration before any validation evaluation."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract


def main():
    contract = load_stage3_contract()
    config = contract["config"]
    step8 = config["step8"]
    target = PROJECT_ROOT / step8["preregistration"]
    if target.exists():
        raise FileExistsError(f"Immutable Step 8 pre-registration: {target}")
    economic_keys = ("signal", "universe", "portfolio", "rebalance", "execution",
                     "accounting", "costs", "benchmarks", "research_protocol", "step8", "objective")
    economic_config = {key: config[key] for key in economic_keys}
    config_sha = canonical_hash(economic_config)
    code_names = ("signals.py", "portfolio.py", "portfolio_build.py", "execution.py", "accounting.py",
                  "r1_engine.py", "execution_build.py", "benchmark.py", "benchmark_build.py",
                  "research.py", "research_build.py", "promotion.py", "validation_build.py", "validation_publish.py")
    code_hashes = {name: file_sha256(PROJECT_ROOT / "src/core/strategy" / name) for name in code_names}
    code_sha = canonical_hash(code_hashes)
    design_manifest = PROJECT_ROOT / config["delivery"]["step7_manifest"]
    identity = {"candidate_id": step8["candidate_id"], "version": step8["candidate_version"],
        "design_run_id": step8["design_run_id"], "design_manifest_sha256": file_sha256(design_manifest),
        "config_sha256": config_sha, "code_bundle_sha256": code_sha,
        "validation_period": config["research_protocol"]["validation_period"],
        "economic_boundaries": step8["economic_boundaries"], "benchmark_ids": ["eligible_passive_net", "common_support_passive_net"]}
    document = {**identity, "freeze_id": "freeze_" + canonical_hash(identity)[:20],
        "input_bundle": config["delivery"]["active_input_bundle"],
        "created_on": "2026-11-02", "frozen_before_validation_evaluation": True,
        "validation_results_accessed_for_candidate_selection": False,
        "engineering_metadata_seen": "sample_end_accounting_quality_only",
        "confirmation_seen": False, "oos_accessed": False,
        "economic_config_sha256_includes": list(economic_keys), "code_sha256": code_hashes,
        "r2_permitted": False, "live_permitted": False}
    for key in ("supersedes_preregistration", "engineering_change_reason"):
        if key in step8:
            document[key] = step8[key]
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".staging")
    if staging.exists():
        raise FileExistsError(f"Pre-registration staging exists: {staging}")
    staging.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    staging.replace(target)
    print(json.dumps({"preregistration": str(target.relative_to(PROJECT_ROOT)),
        "sha256": file_sha256(target), "freeze_id": document["freeze_id"],
        "validation_period": document["validation_period"], "confirmation_seen": False,
        "validation_results_accessed_for_candidate_selection": False}, indent=2))


if __name__ == "__main__":
    main()
