"""Build Step 5 execution events and the R1 research account."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.execution_build import Step5Builder, default_step5_run_id, publish_step5
from core.strategy.inputs import SnapshotInputs


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    run_id = default_step5_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / config["delivery"]["active_input_bundle"]) as inputs:
        builder = Step5Builder(inputs, root / "runs" / config["delivery"]["active_step4_run"], config)
        result = publish_step5(builder, root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "tables": {name: entry["rows"] for name, entry in result["tables"].items()},
                      "accounting_mode": "adjusted_unit_proxy", "real_share_account_ready": False,
                      "research_labels_read": False}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
