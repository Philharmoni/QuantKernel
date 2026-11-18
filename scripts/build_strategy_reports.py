"""Build the Step 9 no-promotion report and evidence package."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.reporting_build import Step9Builder
from core.strategy.reporting_publish import default_step9_run_id, publish_step9


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_roots = {step: root / "runs" / config["delivery"][f"active_step{step}_run"] for step in range(3, 9)}
    run_id = default_step9_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        result = publish_step9(Step9Builder(inputs, config, PROJECT_ROOT, run_roots), root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "decision": result["decision"], "files": result["files"],
        "economic_promoted": False, "r2_backtest_permitted": False,
        "confirmation_seen": False, "oos_accessed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
