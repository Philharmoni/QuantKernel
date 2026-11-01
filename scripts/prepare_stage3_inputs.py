"""Prepare an immutable physical in-sample snapshot, not an OOS evaluation."""
import json

import _bootstrap  # noqa: F401
from core.strategy.inputs import prepare_snapshot


def main():
    result = prepare_snapshot()
    print(json.dumps({"bundle_id": result["bundle_id"], "cutoff": result["cutoff"],
                      "tables": len(result["tables"]), "status": result["status"],
                      "oos_performance_generated": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
