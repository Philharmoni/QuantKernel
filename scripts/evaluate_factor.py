"""Evaluate one computed factor_values.parquet in an isolated experiment directory."""
import argparse
import json
import logging

import _bootstrap  # noqa: F401
from core.factor.experiment import evaluate_experiment


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="factor_values.parquet, experiment.yaml, or experiment directory")
    parser.add_argument("--profile")
    parser.add_argument("--universe", help="Evaluate independently in an existing custom research universe")
    args = parser.parse_args()
    result = evaluate_experiment(args.input, profile=args.profile, universe=args.universe)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
