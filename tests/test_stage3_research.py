import copy
from datetime import date

import pytest

from core.strategy.contract import load_stage3_contract
from core.strategy.r1_engine import R1Engine
from core.strategy.research import extended_performance, monthly_returns, segment_definitions


D1, D2, D3 = date(2020, 1, 31), date(2020, 2, 28), date(2020, 3, 31)


def test_extended_performance_and_monthly_returns_use_continuous_nav():
    rows = [{"trade_date": D1, "reference_nav_cny": 1_100.0},
            {"trade_date": D2, "reference_nav_cny": 1_045.0},
            {"trade_date": D3, "reference_nav_cny": 1_200.0}]
    metrics = extended_performance(rows, 1_000)
    assert metrics["observations"] == 3
    assert metrics["cumulative_return"] == pytest.approx(0.20)
    assert metrics["max_drawdown"] == pytest.approx(-0.05)
    assert metrics["historical_var_95"] == pytest.approx(-0.035)
    assert metrics["historical_es_95"] == pytest.approx(-0.05)
    monthly = monthly_returns(rows, 1_000)
    assert [row["return"] for row in monthly] == pytest.approx([0.10, -0.05, 1_200 / 1_045 - 1])
    assert monthly[-1]["start_nav"] == pytest.approx(1_045)


def test_design_segments_are_fixed_and_non_overlapping_in_identity():
    segments = segment_definitions(date(2019, 7, 1), date(2021, 12, 31))
    assert [item[0] for item in segments] == ["design", "year:2019", "year:2020", "year:2021", "window:2019-2020"]


def test_nav_only_mode_preserves_daily_account_results_without_detail_memory():
    config = copy.deepcopy(load_stage3_contract()["config"])
    config["objective"]["initial_capital_cny"] = 1_000_000
    order = {"order_id": "golden", "signal_date": D1, "planned_execution_date": D1,
             "ts_code": "SA001.SZ", "side": "BUY", "purpose": "rebalance",
             "reference_notional_cny": 100_000.0, "adv20_cny": 20_000_000.0,
             "return_volatility_20": 0.02, "order_status": "intent"}
    fact = {"open": 10.0, "close": 10.0, "adj_factor": 1.0, "amount": 20_000.0,
            "is_st": False, "is_delisted": False, "is_listed": True, "quotation_exists": True,
            "limit_price_exists": True, "up_limit": 11.0, "down_limit": 9.0,
            "is_suspended": False, "is_full_day_suspended": False, "is_intraday_suspended": False}
    provider = lambda day, codes: {code: fact for code in codes}
    full = R1Engine(config, [D1], provider, [order]).run()
    compact = R1Engine(config, [D1], provider, [order], nav_only=True).run()
    assert compact["nav_daily"] == full["nav_daily"]
    assert compact["cost_daily"] == full["cost_daily"]
    assert compact["risk_daily"] == full["risk_daily"]
    for table in ("execution_eligibility", "order_event", "fill", "position_lot", "cash_ledger"):
        assert compact[table] == []
