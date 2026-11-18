from core.strategy.closeout import closeout_status, step_evidence_errors


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
