"""Verify a real Step 3 run, including truncation and independent full repeat."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import load_stage3_contract
from core.strategy.signal_checks import record_signal_checks
from core.strategy.signals import default_run_id


def main():
    config = load_stage3_contract()["config"]
    bundle = config["delivery"]["active_input_bundle"]
    run_id = default_run_id()
    root = PROJECT_ROOT / config["delivery"]["root"]
    result = record_signal_checks(root / "runs" / run_id, root / "inputs" / bundle, config,
                                  truncation_date="20200430",
                                  repeat_root=root / "verification" / (run_id + "_repeat"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
