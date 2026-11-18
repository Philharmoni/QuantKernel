"""Build Step 7 design-period research for the frozen runnable candidate set."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.research_build import Step7Builder
from core.strategy.research_publish import default_step7_run_id, publish_step7


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step7_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        builder = Step7Builder(inputs, root / "runs" / config["delivery"]["active_step3_run"],
            root / "runs" / config["delivery"]["active_step4_run"],
            root / "runs" / config["delivery"]["active_step5_run"], config)
        result = publish_step7(builder, root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "design_period": config["research_protocol"]["design_period"],
        "completed_candidates": result["completed_candidates"], "diagnostics_run": result["diagnostics_run"],
        "tables": {name: entry["rows"] for name, entry in result["tables"].items()},
        "economic_promoted": False, "validation_seen": False, "confirmation_seen": False,
        "oos_accessed": False}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
