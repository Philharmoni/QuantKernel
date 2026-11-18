from datetime import date, timedelta

import pytest

from core.strategy.benchmark import (IdealTargetAccount, market_value_targets, passive_orders,
                                     performance_metrics, relative_metrics)


D1, D2 = date(2020, 3, 31), date(2020, 4, 1)


def test_market_value_targets_and_orders_are_deterministic():
    rows = [{"ts_code": "SA001.SZ", "circ_mv": 100.0, "adv20_cny": 1e8, "return_volatility_20": 0.02},
            {"ts_code": "SA002.SZ", "circ_mv": 300.0, "adv20_cny": 1e8, "return_volatility_20": 0.02}]
    targets = market_value_targets(rows, 0.95)
    assert targets == pytest.approx({"SA001.SZ": 0.2375, "SA002.SZ": 0.7125})
    assert sum(targets.values()) == pytest.approx(0.95)
    key = (D1, D2)
    infos = {key: {row["ts_code"]: row for row in rows}}
    first = passive_orders({key: targets}, infos, 10_000, "eligible_passive")
    second = passive_orders({key: targets}, infos, 10_000, "eligible_passive")
    assert first == second
    assert [order["side"] for order in first] == ["BUY", "BUY"]
    assert sum(order["reference_notional_cny"] for order in first) == pytest.approx(9_500)


def test_ideal_account_respects_target_budget_and_adjusted_factor_valuation():
    targets = {D1: {"SA005.SZ": 0.95}}
    facts = {D1: {"open": 14.0, "close": 14.0, "adj_factor": 1.0},
             D2: {"open": 14.0, "close": 14.0, "adj_factor": 2.0}}
    account = IdealTargetAccount(1_000, [D1, D2], lambda day, codes: {code: facts[day] for code in codes}, targets)
    rows = account.run(D2)
    assert rows[0]["total_cash"] == pytest.approx(50)
    assert rows[0]["reference_nav_cny"] == pytest.approx(1_000)
    assert rows[1]["reference_position_value_cny"] == pytest.approx(1_900)
    assert rows[1]["reference_nav_cny"] == pytest.approx(1_950)


def test_non_overlapping_nav_and_relative_growth():
    strategy = [{"trade_date": D1, "reference_nav_cny": 1_100.0},
                {"trade_date": D2, "reference_nav_cny": 1_045.0}]
    benchmark = [{"trade_date": D1, "reference_nav_cny": 1_000.0},
                 {"trade_date": D2, "reference_nav_cny": 1_000.0}]
    metrics = performance_metrics(strategy, 1_000)
    assert metrics["cumulative_return"] == pytest.approx(0.045)
    relative = relative_metrics(strategy, benchmark)
    assert relative["relative_nav_growth"] == pytest.approx(0.045)


def test_future_quote_perturbation_does_not_change_prior_ideal_nav():
    targets = {D1: {"SA005.SZ": 0.95}, D2: {"SA005.SZ": 0.95}}
    def provider(day, codes, perturb=False):
        multiplier = 5.0 if perturb and day > D1 else 1.0
        return {code: {"open": 10.0 * multiplier, "close": 10.0 * multiplier, "adj_factor": 1.0}
                for code in codes}
    full = IdealTargetAccount(1_000, [D1, D2], lambda day, codes: provider(day, codes), targets).run(D2)
    changed = IdealTargetAccount(1_000, [D1, D2], lambda day, codes: provider(day, codes, True), targets).run(D2)
    assert full[0] == changed[0]
    assert full[1]["reference_nav_cny"] != changed[1]["reference_nav_cny"]
