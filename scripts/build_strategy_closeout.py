"""Build the Step 14 R1 closeout without reading market data."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.closeout_build import Step14Builder, default_step14_run_id, publish_step14
from core.strategy.contract import load_stage3_contract
from core.quality.verification import verification_mode


def main():
    mode = verification_mode()
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / config["delivery"]["root"]
    run_id = default_step14_run_id(use_active=False, verification=mode)
    result = publish_step14(Step14Builder(config, PROJECT_ROOT, verification=mode), root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "closeout_status": result["closeout_status"],
        "evidence_steps": result["evidence_steps"], "gate_count": result["gate_count"],
        "files": result["files"], "economic_promoted": False, "r2_backtest_permitted": False,
        "confirmation_seen": False, "oos_accessed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
