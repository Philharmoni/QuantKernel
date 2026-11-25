"""Verify Stage 1 products and acceptance evidence before any Stage 2 build runs."""
import argparse
import logging

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.factor.entry import check_stage2_entry
from core.quality.verification import add_verification_argument, verification_mode


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile')
    add_verification_argument(parser)
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile), stage='stage2') as store:
        result = check_stage2_entry(store, verification=verification_mode(args.verification))
    print(f"Stage 2 entry gate passed; bundle_sha256={result['bundle_sha256']}; "
          f"{len(result['upstream_tables'])} upstream tables bound")


if __name__ == '__main__':
    main()
