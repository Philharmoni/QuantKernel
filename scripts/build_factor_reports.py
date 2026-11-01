"""Generate deterministic Step 9 per-factor and cross-factor research reports."""
import argparse
import json
import logging

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.store import Store
from core.evaluation.report import build_factor_reports
from core.factor.report import build_factor_coverage_report


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--factor-ids", nargs="+")
    parser.add_argument("--skip-coverage", action="store_true",
                        help="Reuse the existing coverage report instead of rebuilding all factors")
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile)) as store:
        coverage = None
        if not args.skip_coverage:
            coverage = build_factor_coverage_report(store)
        result = build_factor_reports(store, factor_ids=args.factor_ids)
    print(json.dumps({"factors": result["factors"], "files": result["files"],
                      "coverage_factors": None if coverage is None else len(coverage["summary"]),
                      "report_manifest_sha256": result["manifest"]["sources"]["l4/factor_summary"]["sha256"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
