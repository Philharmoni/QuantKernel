"""Promote one evaluated experiment into the accepted factor library."""
import argparse
import json

import _bootstrap  # noqa: F401
from core.factor.promotion import promote_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="factor_values.parquet, experiment.yaml, or experiment directory")
    parser.add_argument("--approve", action="store_true", help="Explicit research approval; required by default policy")
    args = parser.parse_args()
    try:
        result = promote_experiment(args.input, approve=args.approve)
    except ValueError as error:
        print(json.dumps({"promoted": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
