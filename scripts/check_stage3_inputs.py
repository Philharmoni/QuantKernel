"""Validate a Stage 3 physical snapshot without writing any data artifact."""
import argparse
import json

import _bootstrap  # noqa: F401
from core.strategy.admission import check_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_root")
    args = parser.parse_args()
    result = check_snapshot(args.bundle_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
