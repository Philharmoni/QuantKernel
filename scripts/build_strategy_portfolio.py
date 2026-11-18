"""Build Step 4 target portfolios and reference order intents."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.portfolio import default_step4_run_id
from core.strategy.portfolio_build import Step4Builder, publish_step4


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / "data_middle/strategy_research"
    bundle = config["delivery"]["active_input_bundle"]
    signal_run = root / "runs" / config["delivery"]["active_step3_run"]
    run_id = default_step4_run_id(use_active=False)
    with SnapshotInputs(root / "inputs" / bundle) as inputs:
        builder = Step4Builder(inputs, signal_run, config)
        result = publish_step4(builder, root / "runs" / run_id, run_id)
        summary = inputs.db.execute("""SELECT status,count(*) FROM (
            SELECT * FROM read_parquet(?)) GROUP BY status ORDER BY status""",
            [str(root / "runs" / run_id / "l4/portfolio_daily/part-00000.parquet")]).fetchall()
    print(json.dumps({"run_id": run_id, "tables": list(result["tables"]),
                      "daily_status": summary, "research_labels_read": False}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
