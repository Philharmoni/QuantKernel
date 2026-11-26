"""Check Stage 3 Step 0 metadata and protected files; no raw/data parquet reads."""
import json

import _bootstrap  # noqa: F401
from core.config.paths import PROJECT_ROOT
from core.strategy.contract import (architecture_violations, load_stage3_contract,
                                    read_stage2_acceptance_metadata, verify_protected_hashes)
from core.quality.verification import verification_metadata, verification_mode


def main():
    mode = verification_mode()
    contract = load_stage3_contract()
    protected = (verify_protected_hashes() if mode == "audit" else
                 {"passed": True, "status": "not_requested", "checked_files": 0, "differences": []})
    result = {
        "step": 0,
        "passed": False,
        "config_sha256": contract["config_sha256"],
        "registry_sha256": contract["registry_sha256"],
        "acceptance_metadata": read_stage2_acceptance_metadata(),
        "protected_files": protected,
        "architecture_violations": architecture_violations(PROJECT_ROOT / "src/core"),
        "input_status": contract["config"]["delivery"]["current_input_status"],
        "live_ready": False,
        **verification_metadata(mode),
    }
    result["passed"] = (result["protected_files"]["passed"] and not result["architecture_violations"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
