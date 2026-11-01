"""Build Step 7 single-factor L4 evaluation products and append the trial log."""
import argparse
import json
import logging

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.evaluation.builder import evaluate_factors


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--start", help="Inclusive evaluation start, YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--end", help="Inclusive evaluation end, YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--factor-ids", nargs="+")
    parser.add_argument("--process-ids", nargs="+")
    parser.add_argument("--horizons", nargs="+", type=int, choices=[1, 5, 10, 20])
    parser.add_argument("--merge-existing", action="store_true",
                        help="Replace selected factor IDs but preserve other evaluated factors")
    parser.add_argument("--include-out-of-sample", action="store_true",
                        help="Formally access locked OOS data; allowed once per factor version")
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile)) as store:
        result = evaluate_factors(
            store, start=args.start, end=args.end, factor_ids=args.factor_ids,
            process_ids=args.process_ids, horizons=args.horizons,
            include_out_of_sample=args.include_out_of_sample,
            merge_existing=args.merge_existing)
    print(json.dumps({name: {"rows": manifest["rows"], "sha256": manifest["sha256"]}
                      for name, manifest in result.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
