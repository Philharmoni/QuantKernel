"""Build Step 13 design-only R1 robustness diagnostics."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.robustness_build import Step13Builder
from core.strategy.robustness_publish import default_step13_run_id, publish_step13


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step13_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        builder = Step13Builder(inputs, root / "runs" / config["delivery"]["active_step4_run"],
            root / "runs" / config["delivery"]["active_step5_run"],
            root / "runs" / config["delivery"]["active_step7_run"], config)
        result = publish_step13(builder, root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "design_period": result["design_period"],
        "truncation_date": result["truncation_date"],
        "tables": {name: entry["rows"] for name, entry in result["tables"].items()},
        "economic_promoted": False, "r2_backtest_permitted": False,
        "confirmation_seen": False, "oos_accessed": False}, indent=2, default=str))


if __name__ == "__main__":
    main()
