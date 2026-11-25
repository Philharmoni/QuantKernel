"""Execute the Step 10 R2 quality gate without running R2."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.r2_quality_build import R2QualityBuilder, default_step10_run_id, publish_r2_quality


def main():
    config = load_stage3_contract()["config"]
    root = PROJECT_ROOT / config["delivery"]["root"]
    run_id = default_step10_run_id(use_active=False)
    result = publish_r2_quality(R2QualityBuilder(config, PROJECT_ROOT), root / "runs" / run_id, run_id)
    print(json.dumps({"run_id": run_id, "quality_gate_executed": True,
        "r2_quality_gate_passed": result["r2_quality_gate_passed"],
        "r2_backtest_permitted": False, "can_enter_step11": False,
        "files": result["files"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
