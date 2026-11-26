"""Rebuild the implemented Stage 3 R1 workflow without running R2 or OOS.

Examples:
    python scripts/build_stage3.py --verification standard --record-run rebuild
    python scripts/build_stage3.py --dry-run
    python scripts/build_stage3.py --reuse-existing

The R2 step is only its implemented negative quality gate, not an R2 backtest.
Existing immutable outputs require --reuse-existing or a fresh STRATEGY_RESEARCH_ROOT.
"""
import argparse
import json
import logging

import _bootstrap  # noqa: F401
from core.pipeline.stage3 import run_stage3_pipeline
from core.quality.verification import add_verification_argument, verification_mode


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record-run", default="latest", help="Pipeline status record name")
    parser.add_argument("--dry-run", action="store_true", help="Print supported steps without building")
    parser.add_argument("--reuse-existing", action="store_true",
                        help="Reuse existing active outputs and rerun their checks")
    add_verification_argument(parser)
    args = parser.parse_args()
    result = run_stage3_pipeline(record_run=args.record_run, dry_run=args.dry_run,
                                 reuse_existing=args.reuse_existing,
                                 verification=verification_mode(args.verification))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
