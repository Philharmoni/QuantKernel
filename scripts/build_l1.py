"""Build L1 in dependency order, stopping at the first failed component gate."""
import argparse
import logging

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description="Build the reusable L1 data layer")
    parser.add_argument("--profile")
    parser.add_argument("--only", choices=["all", "calendar", "prices", "state", "trading", "available", "quarters"], default="all")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    from core.calendar.trading import build_calendar
    from core.market.prices import build_adjusted_price
    from core.market.state import build_stock_state
    from core.market.trading import build_trade_status
    from core.financial.available import build_financial_available
    from core.financial.quarters import build_quarterly_and_ttm
    from core.quality.checks import check_component
    builders = {
        'calendar': ('daily_calendar', lambda s: build_calendar(s)),
        'prices': ('daily_adjusted_price', lambda s: build_adjusted_price(s, args.start, args.end)),
        'state': ('daily_stock_state', lambda s: build_stock_state(s, args.start, args.end)),
        'trading': ('daily_trade_status', lambda s: build_trade_status(s, args.start, args.end)),
        'available': ('financial_available', lambda s: build_financial_available(s)),
        'quarters': ('quarterly_and_ttm', lambda s: build_quarterly_and_ttm(s)),
    }
    for name in builders if args.only == 'all' else [args.only]:
        component, builder = builders[name]
        with Store(load_paths(profile=args.profile)) as store:
            result = builder(store)
            check_component(store, component, args.start, args.end)
            for manifest in [result] if 'table' in result else result.values():
                print(f"Built {manifest['table']}: {manifest['rows']} rows")


if __name__ == "__main__":
    main()
