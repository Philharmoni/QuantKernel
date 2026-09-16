"""Build the configured, strategy-independent research identity samples."""
import argparse

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.universe.research import build_research_universe
from core.quality.checks import check_component


def main():
    parser = argparse.ArgumentParser(description='Build L2 research identity samples')
    parser.add_argument('--profile')
    parser.add_argument('--start')
    parser.add_argument('--end')
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile)) as store:
        results = build_research_universe(store, args.start, args.end)
        check_component(store, 'research_universe', args.start, args.end)
        for result in results.values():
            print(f"Built {result['table']}: {result['rows']} rows")


if __name__ == '__main__':
    main()
