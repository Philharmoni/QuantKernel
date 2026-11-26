import copy
import json
from pathlib import Path

import pytest

from core.strategy.contract import (ContractError, architecture_violations, assert_accounting_allowed,
                                    assert_date_allowed, assert_label_scope, create_protection_manifest,
                                    load_stage3_contract, read_stage2_acceptance_metadata,
                                    verify_protected_hashes)


@pytest.fixture
def contract():
    return load_stage3_contract()["config"]


def test_step0_freezes_scope_candidates_and_r1_boundary(contract):
    assert contract["approval_scope"]["approved_steps"] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14]
    assert contract["research_protocol"]["max_eligible_candidates"] == 2
    assert contract["research_protocol"]["max_diagnostics"] == 5
    assert contract["accounting"]["mode"] == "adjusted_unit_proxy"
    convention = contract["research_protocol"]["backtest_convention"]
    assert convention["default_tier"] == "R1"
    assert convention["accounting_mode"] == "adjusted_unit_proxy"
    assert convention["fill_model"] == "proxy_not_observed"
    assert convention["r2_requires_separate_quality_gate_and_authorization"] is True
    assert convention["live_trading"] is False
    assert contract["delivery"]["current_input_status"] == "admitted_physical_snapshot"
    assert contract["delivery"]["active_input_bundle"] == "in_sample_65efe5cadd765004"
    assert isinstance(contract["delivery"]["engineering_passed"], bool)
    for key in ("economic_promoted", "real_share_account_ready", "live_ready", "oos_accessed"):
        assert contract["delivery"][key] is False


def test_candidate_run_layout_groups_every_active_step(contract):
    delivery = contract["delivery"]
    group = delivery["active_run_group"]
    assert delivery["run_layout"] == "candidate_step_v2"
    assert group == "c0_three_module_fixed"
    for step in (3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
        assert delivery[f"active_step{step}_run"] == group + "/step" + str(step)
        assert delivery[f"step{step}_manifest"] == (
            "strategy_research/runs/" + group + "/step" + str(step) + "/manifest.json")


def test_oos_default_and_actual_label_endpoints_are_enforced(contract):
    for field in ("trade_date", "entry_date", "exit_date"):
        with pytest.raises(PermissionError, match=field):
            assert_label_scope({"trade_date": "20241231", "entry_date": "20241231",
                                "exit_date": "20241231", field: "20250102"}, contract)
    assert_label_scope({"trade_date": "20241230", "entry_date": "20241231",
                        "exit_date": "20241231"}, contract)
    with pytest.raises(ContractError, match="after"):
        assert_label_scope({"trade_date": "20241231", "exit_date": "20241231"}, contract)


@pytest.mark.parametrize("value", ["2025-01-01", "20250101", "20250102"])
def test_oos_rejects_compact_and_iso_dates(contract, value):
    with pytest.raises(PermissionError):
        assert_date_allowed(value, contract)


def test_r2_is_blocked_without_accepted_company_actions(contract):
    with pytest.raises(PermissionError, match="accepted_company_action_ledger"):
        assert_accounting_allowed(contract, "real_share_account_net")


def test_no_raw_access_or_label_import_in_downstream_modules(tmp_path):
    (tmp_path / "strategy").mkdir()
    (tmp_path / "execution").mkdir()
    (tmp_path / "strategy/signal.py").write_text("def x(store):\n    return store.raw('daily')\n")
    (tmp_path / "execution/fill.py").write_text("from core.label.forward import build\n")
    assert sorted(architecture_violations(tmp_path)) == [
        "label_import:execution/fill.py", "raw_access:strategy/signal.py"]


def test_stage2_acceptance_is_metadata_only_and_not_rerun():
    result = read_stage2_acceptance_metadata()
    assert result["passed"] and len(result["checks"]) == 8
    assert result["oos_formal_factors"] == []


def test_protected_hash_guard_detects_change(tmp_path):
    path = tmp_path / "protected.txt"
    path.write_text("original")
    saved = tmp_path / "saved.json"
    from core.strategy.contract import file_sha256
    saved.write_text(json.dumps({"files": {"protected.txt": file_sha256(path)}}))
    path.write_text("changed")
    result = verify_protected_hashes(tmp_path, saved)
    assert result == {"passed": False, "checked_files": 1,
                      "differences": [{"path": "protected.txt", "reason": "changed"}]}


def test_contract_loader_does_not_require_markdown(tmp_path):
    (tmp_path / "configs").mkdir()
    for name in ("strategy_research.yaml", "strategy_registry.yaml"):
        (tmp_path / "configs" / name).write_text(Path("configs", name).read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    loaded = load_stage3_contract(tmp_path)
    assert loaded["config"]["status"] == "frozen_step_0"
    assert "plan_contract_sha256" not in loaded["config"]


def test_contract_loader_uses_local_active_input_pointer(tmp_path):
    (tmp_path / "configs").mkdir()
    for name in ("strategy_research.yaml", "strategy_registry.yaml"):
        (tmp_path / "configs" / name).write_text(Path("configs", name).read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    pointer = tmp_path / "strategy_research/inputs/active_input.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"version": 1, "bundle_id": "in_sample_new123"}), encoding="utf-8")
    delivery = load_stage3_contract(tmp_path)["config"]["delivery"]
    assert delivery["active_input_bundle"] == "in_sample_new123"
    assert delivery["input_bundle_manifest"] == "strategy_research/inputs/in_sample_new123/manifest.json"
    assert delivery["input_bundle_admission"] == "strategy_research/inputs/in_sample_new123/admission.json"


def test_protected_hash_guard_ignores_but_does_not_rewrite_markdown(tmp_path):
    markdown = tmp_path / "spec.md"
    protected = tmp_path / "protected.txt"
    markdown.write_text("old")
    protected.write_text("original")
    from core.strategy.contract import file_sha256
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps({"files": {"spec.md": file_sha256(markdown),
                                           "protected.txt": file_sha256(protected)}}))
    markdown.unlink()
    assert verify_protected_hashes(tmp_path, saved) == {"passed": True, "checked_files": 1,
                                                        "differences": []}
    protected.write_text("changed")
    assert verify_protected_hashes(tmp_path, saved)["differences"] == [
        {"path": "protected.txt", "reason": "changed"}]


def test_protection_manifest_excludes_nested_markdown(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "docs/specs").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("root")
    (tmp_path / "docs/specs/spec.md").write_text("nested")
    (tmp_path / "docs/rules.yaml").write_text("rule: true")
    (tmp_path / "src/rule.py").write_text("pass")
    (tmp_path / "configs/strategy_research.yaml").write_text(
        "protected_scope:\n  scope: test\n  paths: [README.md, docs, src]\n  excludes: []\n")
    result = create_protection_manifest(tmp_path)
    assert set(result["files"]) == {"docs/rules.yaml", "src/rule.py"}


def test_registry_and_config_reject_unapproved_candidate(contract):
    loaded = load_stage3_contract()
    changed = copy.deepcopy(loaded["config"])
    changed["research_protocol"]["eligible_candidate_ids"].append("unapproved")
    from core.strategy.contract import validate_contract
    assert "Strategy registry and candidate budget disagree" in validate_contract(changed, loaded["registry"])
