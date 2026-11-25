"""One-command Stage 2 rebuild: Step 0-10, repeatability evidence and final gate."""
import argparse
import json
import logging

import _bootstrap  # noqa: F401
from core.pipeline.stage2 import run_stage2_pipeline
from core.quality.verification import add_verification_argument, verification_mode


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--record-run", default="latest",
                        help="Save factor_research/verification/runs/stage2_NAME.json (letters, digits, _ and -)")
    parser.add_argument("--deep-primary-keys", action="store_true",
                        help="Re-scan duplicate/null keys across all artifacts during the final gate")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the canonical steps without reading or writing any data")
    add_verification_argument(parser)
    args = parser.parse_args()
    result = run_stage2_pipeline(profile=args.profile, record_run=args.record_run,
                                 dry_run=args.dry_run, deep_primary_keys=args.deep_primary_keys,
                                 verification=verification_mode(args.verification))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("passed", result.get("dry_run", False)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
