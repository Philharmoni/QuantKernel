from datetime import date, timedelta

import pytest

from core.strategy.accounting import R1Account
from core.strategy.robustness import PITFacts, prefix_comparison, pressure_factor, stress_position_value


def test_pressure_factors_cover_60_120_and_delisting_boundaries():
    assert pressure_factor(59) == 1.0
    assert pressure_factor(60) == 0.5
    assert pressure_factor(119) == 0.5
    assert pressure_factor(120) == 0.0
    assert pressure_factor(0, non_tradable=True) == 0.0
    assert stress_position_value(200, 60) == 100.0
    assert stress_position_value(200, 120) == 0.0


def test_pit_facts_blocks_future_before_calling_provider():
    calls = []

    def provider(day, codes):
        calls.append((day, tuple(codes)))
        return {"X": {"close": 1.0}}

    cutoff = date(2020, 12, 31)
    facts = PITFacts(provider, cutoff)
    assert facts(cutoff, ["X"])["X"]["close"] == 1.0
    with pytest.raises(PermissionError, match="Future fact read blocked"):
        facts(cutoff + timedelta(days=1), ["X"])
    assert calls == [(cutoff, ("X",))]


def test_prefix_comparison_handles_dates_and_separates_future_rows():
    full = [{"trade_date": date(2020, 12, 30), "reference_nav_cny": 100.0},
            {"trade_date": date(2020, 12, 31), "reference_nav_cny": 101.0},
            {"trade_date": date(2021, 1, 4), "reference_nav_cny": 102.0}]
    result = prefix_comparison(full, full[:2], date(2020, 12, 31))
    assert result["prefix_equal"] and result["mismatch_count"] == 0
    assert result["full_prefix_sha256"] == result["variant_sha256"]
    perturbed = prefix_comparison(full, full, date(2020, 12, 31))
    assert perturbed["prefix_equal"] and perturbed["mismatch_count"] == 0
    assert perturbed["variant_future_rows"] == 1


def test_stale_marks_use_last_quote_without_future_backfill_or_cash_release():
    account = R1Account({"accounting": {"mode": "adjusted_unit_proxy"},
                         "objective": {"initial_capital_cny": 1_000}})
    account.apply_fill({"ts_code": "X", "side": "BUY", "units": 10.0, "notional_cny": 100.0,
                        "total_cost_cny": 0.0, "fill_id": "f1", "execution_date": date(2020, 1, 2),
                        "raw_open": 10.0, "adj_factor": 1.0}, date(2020, 1, 3))
    cash = account.total_cash
    quoted = account.mark_and_value(date(2020, 1, 3), {
        "X": {"quotation_exists": True, "close": 12.0, "adj_factor": 1.0,
              "is_delisted": False, "is_listed": True}})
    assert quoted["reference_position_value_cny"] == 120.0
    lot = account.lots["X"][0]
    lot["stale_days"] = 59
    stale_60 = account.mark_and_value(date(2020, 4, 1), {"X": {}})
    assert stale_60["reference_position_value_cny"] == 120.0
    assert stale_60["stress_position_value_cny"] == 60.0
    lot["stale_days"] = 119
    stale_120 = account.mark_and_value(date(2020, 7, 1), {"X": {}})
    assert stale_120["stress_position_value_cny"] == 0.0
    delisted = account.mark_and_value(date(2020, 7, 2), {"X": {"is_delisted": True}})
    assert delisted["reference_position_value_cny"] == 120.0
    assert delisted["stress_position_value_cny"] == 0.0
    assert account.total_cash == cash
