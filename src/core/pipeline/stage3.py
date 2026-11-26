"""Fail-fast orchestration for the implemented Stage 3 R1 research workflow."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from core.config.paths import PROJECT_ROOT
from core.quality.verification import verification_metadata, verification_mode
from core.strategy.contract import load_stage3_contract


PIPELINE_STEPS = (
    "step00_contract",
    "step02_input_snapshot",
    "step03_signals",
    "step04_portfolio",
    "step05_execution",
    "step06_benchmark",
    "step07_design_research",
    "step08_freeze_and_validation",
    "step09_reports",
    "step10_r2_negative_gate",
    "step12_sensitivity",
    "step13_robustness",
    "step14_r1_closeout",
)


def pipeline_steps() -> tuple[str, ...]:
    return PIPELINE_STEPS


def _steps(config):
    delivery = config["delivery"]

    def step_output(step):
        return delivery[f"step{step}_manifest"]

    return [
        {"name": "step00_contract", "build": [], "check": "check_stage3_contract.py", "output": None},
        {"name": "step02_input_snapshot",
         "build": [("prepare_stage3_inputs.py", delivery["input_bundle_manifest"])],
         "check": "check_stage3_inputs.py", "output": delivery["input_bundle_manifest"]},
        {"name": "step03_signals", "build": [("build_strategy_signals.py", step_output(3))],
         "check": "check_stage3_signals.py", "output": step_output(3)},
        {"name": "step04_portfolio", "build": [("build_strategy_portfolio.py", step_output(4))],
         "check": "check_stage3_portfolio.py", "output": step_output(4)},
        {"name": "step05_execution", "build": [("build_strategy_execution.py", step_output(5))],
         "check": "check_stage3_execution.py", "output": step_output(5)},
        {"name": "step06_benchmark", "build": [("build_strategy_benchmark.py", step_output(6))],
         "check": "check_stage3_benchmark.py", "output": step_output(6)},
        {"name": "step07_design_research", "build": [("build_strategy_research.py", step_output(7))],
         "check": "check_stage3_research.py", "output": step_output(7)},
        {"name": "step08_freeze_and_validation",
         "build": [("freeze_strategy_candidate.py", config["step8"]["preregistration"]),
                   ("build_strategy_validation.py", step_output(8))],
         "check": "check_stage3_validation.py", "output": step_output(8)},
        {"name": "step09_reports", "build": [("build_strategy_reports.py", step_output(9))],
         "check": "check_stage3_reports.py", "output": step_output(9)},
        {"name": "step10_r2_negative_gate", "build": [("build_r2_quality_gate.py", step_output(10))],
         "check": "check_r2_quality_gate.py", "output": step_output(10)},
        {"name": "step12_sensitivity", "build": [("build_strategy_sensitivity.py", step_output(12))],
         "check": "check_stage3_sensitivity.py", "output": step_output(12)},
        {"name": "step13_robustness", "build": [("build_strategy_robustness.py", step_output(13))],
         "check": "check_stage3_robustness.py", "output": step_output(13)},
        {"name": "step14_r1_closeout", "build": [("build_strategy_closeout.py", step_output(14))],
         "check": "check_stage3_closeout.py", "output": step_output(14)},
    ]


def _write_status(project_root, config, record_run, payload):
    root = Path(project_root) / config["delivery"]["root"] / "verification" / "pipeline"
    root.mkdir(parents=True, exist_ok=True)
    targets = (root / f"stage3_{record_run}.json", root / "stage3_pipeline_status.json")
    for target in targets:
        temporary = target.with_name("." + target.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        os.replace(temporary, target)


def run_stage3_pipeline(project_root=PROJECT_ROOT, record_run="latest", dry_run=False,
                        reuse_existing=False, verification="standard"):
    mode = verification_mode(verification)
    project_root = Path(project_root)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", record_run):
        raise ValueError("record_run accepts only letters, digits, underscore and hyphen")
    if dry_run:
        return {"dry_run": True, "steps": list(pipeline_steps()), "reuse_existing": reuse_existing,
                "requires": ["Stage 1 and Stage 2 outputs", "passed Stage 2 quality metadata"],
                "r2_negative_gate_only": True, "r2_backtest": False, "step11": False,
                "confirmation_evaluation": False, "oos_evaluation": False, "live_trading": False,
                **verification_metadata(mode)}

    contract = load_stage3_contract(project_root)
    config = contract["config"]
    steps = _steps(config)
    environment = os.environ.copy()
    environment["QUANTKERNEL_VERIFICATION"] = mode
    started = time.perf_counter()
    stages = []
    current = "initialization"
    _write_status(project_root, config, record_run,
                  {"status": "running", "run": record_run, "stages": stages, **verification_metadata(mode)})

    def execute(script):
        command = [sys.executable, str(project_root / "scripts" / script)]
        subprocess.run(command, cwd=project_root, env=environment, check=True)

    try:
        for step in steps:
            current = step["name"]
            output = project_root / step["output"] if step["output"] else None
            reused = bool(reuse_existing and output and output.is_file())
            if not reused:
                for script, marker in step["build"]:
                    if reuse_existing and marker and (project_root / marker).is_file():
                        continue
                    execute(script)
            execute(step["check"])
            stages.append({"stage": current, "passed": True, "reused_existing": reused})
            _write_status(project_root, config, record_run,
                          {"status": "running", "run": record_run, "stages": stages,
                           **verification_metadata(mode)})
        result = {"status": "passed", "passed": True, "run": record_run, "stages": stages,
                  "elapsed_seconds": round(time.perf_counter() - started, 3),
                  "r2_negative_gate_only": True, "r2_backtest": False, "step11": False,
                  "confirmation_evaluation": False, "oos_evaluation": False, "live_trading": False,
                  **verification_metadata(mode)}
        _write_status(project_root, config, record_run, result)
        return result
    except Exception as error:
        _write_status(project_root, config, record_run,
                      {"status": "failed", "run": record_run, "stage": current,
                       "error": str(error), "stages": stages, **verification_metadata(mode)})
        raise
