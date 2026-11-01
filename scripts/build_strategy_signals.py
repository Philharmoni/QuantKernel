"""Build Stage 3 Step 3 L3 eligibility/features/signals from an admitted snapshot."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.signals import default_run_id, publish_signals


def main():
    contract = load_stage3_contract()["config"]
    bundle = contract["delivery"]["active_input_bundle"]
    bundle_root = PROJECT_ROOT / "data_middle/strategy_research/inputs" / bundle
    run_id = default_run_id()
    output = PROJECT_ROOT / "data_middle/strategy_research/runs" / run_id
    with SnapshotInputs(bundle_root) as inputs:
        result = publish_signals(inputs, output, contract, run_id=run_id)
        summary = inputs.db.execute("""SELECT count(*),sum(day_gate_pass::INT),
            avg(common_factor_coverage),min(other_eligible_count),max(other_eligible_count)
            FROM strategy_signal_daily""").fetchone()
    print(json.dumps({"run_id": run_id, "as_of": result["as_of"], "tables": list(result["tables"]),
                      "research_labels_read": False,
                      "daily_summary": {"days": summary[0], "open_days": summary[1],
                                        "mean_factor_coverage": summary[2], "min_other_eligible": summary[3],
                                        "max_other_eligible": summary[4]}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
