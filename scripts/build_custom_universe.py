"""Build an immutable Stage 1 custom daily research universe.

Input CSV/Parquet must contain trade_date and ts_code; each row is a member on
that day. stock_name is optional input and is always replaced by the historical
name from daily_stock_state.

Example:
    python scripts/build_custom_universe.py members.csv --universe-id my_pool_v1
"""
import argparse
import json

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.universe.custom import build_custom_research_universe, validate_custom_research_universe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Member CSV or Parquet containing trade_date and ts_code")
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    paths = load_paths(profile=args.profile)
    with Store(paths) as store:
        result = build_custom_research_universe(store, args.input, args.universe_id,
                                                start=args.start, end=args.end)
        quality = validate_custom_research_universe(store, args.universe_id)
    if not quality["passed"]:
        raise ValueError(f"Custom universe validation failed: {quality['failures']}")
    print(json.dumps({"universe_id": args.universe_id, "passed": True,
                      "rows": result["universe"]["rows"],
                      "output": str(paths.output("l2", "custom_research_universe")),
                      "quality": quality}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
