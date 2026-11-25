"""Validate a Stage 3 physical snapshot without writing any data artifact."""
import argparse
import json

import _bootstrap  # noqa: F401
from core.strategy.admission import check_snapshot
from core.quality.verification import add_verification_argument, verification_mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_root")
    add_verification_argument(parser)
    args = parser.parse_args()
    result = check_snapshot(args.bundle_root, verification=verification_mode(args.verification))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
