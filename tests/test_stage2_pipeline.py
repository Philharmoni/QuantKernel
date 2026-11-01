"""Stage 2 one-command orchestration contract and repeat-evidence checks."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.config.paths import PROJECT_ROOT
from core.pipeline.stage2 import PIPELINE_STEPS, _compare_manifest_pair, run_stage2_pipeline


def test_dry_run_covers_build_repeat_and_final_gate_without_data_access():
    result = run_stage2_pipeline(profile="does_not_exist", dry_run=True)
    assert result["dry_run"] and result["steps"] == PIPELINE_STEPS
    assert result["steps"][0] == "step00_stage1_admission"
    assert result["steps"][-1] == "step10_raw_unchanged_and_unified_quality"
    assert {"repeat_step7", "repeat_step8", "repeat_step9"} <= set(result["steps"])
    assert not result["network_downloads"] and not result["out_of_sample_evaluation"]
    with pytest.raises(ValueError, match="record_run"):
        run_stage2_pipeline(record_run="bad/name", dry_run=True)


def test_one_command_dry_run_cli():
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_stage2.py"), "--dry-run"],
        cwd=PROJECT_ROOT, check=True, capture_output=True, text=True)
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] and len(payload["steps"]) == len(PIPELINE_STEPS)


def test_repeat_manifest_comparison_requires_rows_and_sha(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    for root in (first, second):
        path = root / "l4" / "factor_summary"
        path.mkdir(parents=True)
        (path / "manifest.json").write_text(json.dumps({"rows": 10, "sha256": "same"}), encoding="utf-8")
    artifacts = [("l4", "factor_summary")]
    assert _compare_manifest_pair(first, second, artifacts)["l4/factor_summary"]["identical"]
    (second / "l4" / "factor_summary" / "manifest.json").write_text(
        json.dumps({"rows": 10, "sha256": "different"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Repeatability mismatch"):
        _compare_manifest_pair(first, second, artifacts)
