from core.strategy.r2_quality import evaluate_r2_gate, quality_report


def ready_inventory():
    return {"r1_evidence_complete": True, "champion_available": True,
            "company_action_ledger": "available", "volume_unit_validated": True,
            "board_lot_rules_complete": True, "settlement_rules_complete": True,
            "announcement_calendar": "available", "delisting_mapping_complete": True,
            "real_share_accounting_authorized": True}


def test_no_champion_independently_blocks_r2_even_with_complete_inputs():
    inventory = ready_inventory()
    inventory["champion_available"] = False
    gate = evaluate_r2_gate(inventory)
    assert gate["passed"] is False
    assert gate["blockers"] == ["champion_available"]
    assert gate["r2_backtest_permitted"] is False
    assert gate["can_enter_step11"] is False


def test_missing_company_actions_and_units_block_and_are_reported():
    inventory = ready_inventory()
    inventory["company_action_ledger"] = "not_available"
    inventory["volume_unit_validated"] = False
    gate = evaluate_r2_gate(inventory)
    assert set(gate["blockers"]) == {"company_action_ledger", "volume_units_validated"}
    report = quality_report(gate, inventory)
    assert "R2 回测未获准，未执行" in report
    assert "company_action_ledger" in report
    assert "volume_units_validated" in report


def test_input_readiness_does_not_claim_real_share_account_or_live_ready():
    gate = evaluate_r2_gate(ready_inventory())
    assert gate["passed"] is True
    assert gate["real_share_account_ready"] is False
    assert gate["live_ready"] is False
