import json
from pathlib import Path

from core.pipeline import stage3 as pipeline
from core.strategy.contract import load_stage3_contract


EXPECTED_COMMANDS = [
    "check_stage3_contract.py",
    "prepare_stage3_inputs.py", "check_stage3_inputs.py",
    "build_strategy_signals.py", "check_stage3_signals.py",
    "build_strategy_portfolio.py", "check_stage3_portfolio.py",
    "build_strategy_execution.py", "check_stage3_execution.py",
    "build_strategy_benchmark.py", "check_stage3_benchmark.py",
    "build_strategy_research.py", "check_stage3_research.py",
    "freeze_strategy_candidate.py", "build_strategy_validation.py", "check_stage3_validation.py",
    "build_strategy_reports.py", "check_stage3_reports.py",
    "build_r2_quality_gate.py", "check_r2_quality_gate.py",
    "build_strategy_sensitivity.py", "check_stage3_sensitivity.py",
    "build_strategy_robustness.py", "check_stage3_robustness.py",
    "build_strategy_closeout.py", "check_stage3_closeout.py",
]


def test_dry_run_contains_only_supported_r1_steps(tmp_path):
    result = pipeline.run_stage3_pipeline(project_root=tmp_path, dry_run=True)
    assert result["steps"] == list(pipeline.PIPELINE_STEPS)
    assert result["r2_negative_gate_only"] is True
    assert result["r2_backtest"] is False
    assert result["step11"] is False
    assert result["confirmation_evaluation"] is False
    assert result["oos_evaluation"] is False
    assert not any("step11" in name for name in result["steps"])


def test_pipeline_executes_build_and_check_scripts_in_order(tmp_path, monkeypatch):
    contract = load_stage3_contract()
    monkeypatch.setattr(pipeline, "load_stage3_contract", lambda root: contract)
    monkeypatch.setattr(pipeline, "_write_status", lambda *args: None)
    calls = []

    def run(command, cwd, env, check):
        calls.append({"script": Path(command[1]).name, "cwd": cwd, "verification": env["QUANTKERNEL_VERIFICATION"],
                      "check": check})

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    result = pipeline.run_stage3_pipeline(project_root=tmp_path, record_run="test")
    assert [call["script"] for call in calls] == EXPECTED_COMMANDS
    assert all(call["verification"] == "standard" and call["check"] for call in calls)
    assert result["passed"] and result["r2_backtest"] is False


def test_reuse_existing_skips_only_completed_build_outputs(tmp_path, monkeypatch):
    contract = load_stage3_contract()
    monkeypatch.setattr(pipeline, "load_stage3_contract", lambda root: contract)
    monkeypatch.setattr(pipeline, "_write_status", lambda *args: None)
    existing = tmp_path / contract["config"]["delivery"]["step3_manifest"]
    existing.parent.mkdir(parents=True)
    existing.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(pipeline.subprocess, "run",
                        lambda command, **kwargs: calls.append(Path(command[1]).name))
    result = pipeline.run_stage3_pipeline(project_root=tmp_path, record_run="test", reuse_existing=True)
    assert "build_strategy_signals.py" not in calls
    assert "check_stage3_signals.py" in calls
    assert "build_strategy_portfolio.py" in calls
    signals = next(item for item in result["stages"] if item["stage"] == "step03_signals")
    assert signals["reused_existing"] is True
