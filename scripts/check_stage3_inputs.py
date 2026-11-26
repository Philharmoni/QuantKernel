"""Validate a Stage 3 physical snapshot without writing any data artifact."""
import argparse
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.admission import check_snapshot
from core.strategy.contract import load_stage3_contract
from core.quality.verification import add_verification_argument, verification_mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_root", nargs="?")
    add_verification_argument(parser)
    args = parser.parse_args()
    bundle_root = args.bundle_root
    if bundle_root is None:
        config = load_stage3_contract()["config"]
        bundle_root = PROJECT_ROOT / config["delivery"]["root"] / "inputs" / config["delivery"]["active_input_bundle"]
    result = check_snapshot(bundle_root, verification=verification_mode(args.verification))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
