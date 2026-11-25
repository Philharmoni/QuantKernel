"""Run the Step 10 unified Stage 2 quality gate and write JSON/Markdown reports."""
import argparse
import json
import logging

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.quality.stage2 import run_stage2_quality_gate
from core.quality.verification import add_verification_argument, verification_mode


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--truncate-dates", nargs="+",
                        help="Override configs/factor_research.yaml quality_gate.truncation_dates")
    parser.add_argument("--deep-primary-keys", action="store_true",
                        help="Re-scan duplicate/null primary keys in addition to current-file integrity")
    add_verification_argument(parser)
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile), stage="stage2") as store:
        report = run_stage2_quality_gate(store, truncation_dates=args.truncate_dates,
                                         deep_primary_keys=args.deep_primary_keys,
                                         verification=verification_mode(args.verification))
    print(json.dumps({"passed": report["passed"],
                      "checks": {name: check["passed"] for name, check in report["checks"].items()},
                      "failure_count": len(report["failures"]),
                      "boundaries": report["boundaries"]}, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
