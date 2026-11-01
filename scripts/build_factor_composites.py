"""Build Step 8 correlations, transparent composites, and processed panels."""
import argparse
import json
import logging

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.evaluation.composite import build_composite_factors, composition_settings
from core.evaluation.correlation import build_factor_correlation
from core.factor.truncate import check_truncation_invariance
from core.preprocess.panel import build_factor_processed


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--start", help="Composite output start; training still uses prior realized IC")
    parser.add_argument("--end", help="Composite output end")
    parser.add_argument("--skip-correlation", action="store_true")
    parser.add_argument("--skip-preprocess", action="store_true")
    parser.add_argument("--truncate-dates", nargs="+")
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile)) as store:
        _, composition, _ = composition_settings()
        composite_ids = [spec["composite_id"] for spec in composition["composites"]]
        correlation = None
        if not args.skip_correlation:
            correlation = build_factor_correlation(store, args.start, args.end)
        result = build_composite_factors(store, args.start, args.end)
        processed = None
        if not args.skip_preprocess:
            processed = build_factor_processed(
                store, args.start, args.end, factor_ids=composite_ids, merge_existing=True)
        truncation = None
        if args.truncate_dates:
            truncation = check_truncation_invariance(store, args.truncate_dates,
                                                     factor_ids=composite_ids)
            store.write_json(("verification", "step8_truncation.json"), truncation)
            if not truncation["passed"]:
                raise ValueError(f"Step 8 truncation gate failed: {truncation['mismatches'][:5]}")
    print(json.dumps({
        "correlation": None if correlation is None else {
            "rows": correlation["manifest"]["rows"], "clusters": correlation["clusters"],
            "redundant_pairs": correlation["redundant_pairs"]},
        "factor_ic_realized": result["factor_ic_realized"]["rows"],
        "factor_composite_weight": result["factor_composite_weight"]["rows"],
        "factor_composite_value": result["factor_composite_value"]["rows"],
        "factor_value": result["factor_value"]["rows"],
        "factor_processed": None if processed is None else processed["rows"],
        "truncation_passed": None if truncation is None else truncation["passed"],
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
