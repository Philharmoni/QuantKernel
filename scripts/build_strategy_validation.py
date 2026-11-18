"""Evaluate the pre-registered candidate on the Step 8 validation period."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.validation_build import Step8Builder
from core.strategy.validation_publish import default_step8_run_id, publish_step8


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step8_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        builder = Step8Builder(inputs, root / "runs" / config["delivery"]["active_step3_run"],
            root / "runs" / config["delivery"]["active_step4_run"], root / "runs" / config["delivery"]["active_step5_run"],
            root / "runs" / config["delivery"]["active_step7_run"], config, PROJECT_ROOT)
        result = publish_step8(builder, root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "candidate_id": result["candidate_id"],
        "validation_period": result["validation_period"], "promotion_status": result["promotion_status"],
        "research_verification_passed": result["research_verification_passed"],
        "tables": {name: entry["rows"] for name, entry in result["tables"].items()},
        "confirmation_seen": False, "oos_accessed": False, "economic_promoted": False}, indent=2, default=str))


if __name__ == "__main__":
    main()
