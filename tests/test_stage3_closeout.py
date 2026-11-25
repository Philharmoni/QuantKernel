import json

from core.strategy.closeout import closeout_status, step_evidence_errors
from core.strategy.closeout_build import independent_repeat_complete
from core.strategy.verification import resolve_repeat, table_repeat_mismatches


def negative_decision():
    return {"decision": "no_champion_no_promotion", "economic_promoted": False}


def negative_r2_gate():
    return {"passed": False, "r2_backtest_permitted": False, "can_enter_step11": False}


def test_closeout_preserves_no_champion_and_r2_block():
    result = closeout_status(negative_decision(), negative_r2_gate(), True)
    assert result["r1_research_complete"] and result["engineering_passed"]
    assert not result["economic_promoted"]
    assert not result["r2_backtest_permitted"] and not result["can_enter_step11"]
    assert not result["real_share_account_ready"] and not result["live_ready"]


def test_closeout_evidence_failure_does_not_create_r2_permission():
    result = closeout_status(negative_decision(), negative_r2_gate(), False)
    assert not result["r1_research_complete"] and not result["engineering_passed"]
    assert not result["r2_backtest_permitted"]


def test_step_evidence_rejects_run_mismatch_and_forbidden_scope():
    manifest = {"step": 13, "run_id": "actual", "verification": {"passed": True}}
    verification = {"step": 13, "run_id": "other", "passed": True, "failures": [], "oos_accessed": True}
    assert step_evidence_errors(13, "actual", manifest, verification) == ["forbidden_scope", "run_identity"]


def test_repeat_policy_does_not_publish_in_standard_or_fast(tmp_path):
    root = tmp_path / "repeat"
    calls = []
    publisher = lambda: calls.append(True) or {"tables": {}, "files": {}}
    assert resolve_repeat(root, publisher, "standard") == (None, "not_requested")
    assert resolve_repeat(root, publisher, "fast") == (None, "skipped")
    assert calls == []
    manifest, status = resolve_repeat(root, publisher, "audit")
    assert manifest == {"tables": {}, "files": {}} and status == "executed" and calls == [True]


def test_standard_uses_existing_repeat_without_publishing(tmp_path):
    root = tmp_path / "repeat"
    root.mkdir()
    expected = {"tables": {"x": {"sha256": "same"}}, "files": {}}
    (root / "manifest.json").write_text(json.dumps(expected), encoding="utf-8")
    actual, status = resolve_repeat(root, lambda: (_ for _ in ()).throw(AssertionError()), "standard")
    assert actual == expected and status == "verified_existing"
    assert table_repeat_mismatches(expected, actual, ["x"]) == []


def test_g10_requires_audit_repeat_evidence():
    assert independent_repeat_complete({"3": {"verification": "audit", "repeat_status": "executed"}})
    assert independent_repeat_complete({"3": {"verification": "audit", "repeat_status": "verified_existing"}})
    assert not independent_repeat_complete({"3": {"verification": "standard", "repeat_status": "not_requested"}})
    assert not independent_repeat_complete({"3": {"verification": "fast", "repeat_status": "skipped"}})
