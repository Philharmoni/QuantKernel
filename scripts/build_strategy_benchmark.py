"""Build the frozen Step 6 benchmark and small-window accounts."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.benchmark_build import Step6Builder
from core.strategy.benchmark_publish import default_step6_run_id, publish_step6
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step6_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        builder = Step6Builder(inputs, root / "runs" / config["delivery"]["active_step3_run"],
            root / "runs" / config["delivery"]["active_step4_run"],
            root / "runs" / config["delivery"]["active_step5_run"], config)
        result = publish_step6(builder, root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "small_window": config["step6"]["small_window"],
        "tables": {name: entry["rows"] for name, entry in result["tables"].items()},
        "economic_promoted": False, "research_labels_read": False}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
