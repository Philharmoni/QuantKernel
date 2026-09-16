import argparse
import logging

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.quality.checks import check_data_quality


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description='Validate L0/L1/L2 and publish an actionable quality report')
    parser.add_argument('--profile')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--l0-scope', choices=['full', 'core'], default='full')
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile)) as store:
        report = check_data_quality(store, args.start, args.end, args.l0_scope)
        print(f"Supported scope passed: {report['passed']}; full phase passed: {report['full_phase_passed']}")


if __name__ == '__main__':
    main()
