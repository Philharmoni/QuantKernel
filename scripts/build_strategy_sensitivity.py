"""Build Step 12 design-only R1 sensitivity diagnostics."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.sensitivity_build import Step12Builder
from core.strategy.sensitivity_publish import default_step12_run_id, publish_step12


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step12_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        builder = Step12Builder(inputs, root / "runs" / config["delivery"]["active_step4_run"],
            root / "runs" / config["delivery"]["active_step5_run"], root / "runs" / config["delivery"]["active_step7_run"], config)
        result = publish_step12(builder, root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "design_period": result["design_period"],
        "tables": {name: entry["rows"] for name, entry in result["tables"].items()},
        "economic_promoted": False, "r2_backtest_permitted": False,
        "confirmation_seen": False, "oos_accessed": False}, indent=2, default=str))


if __name__ == "__main__":
    main()
