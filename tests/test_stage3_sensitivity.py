from datetime import date, timedelta

import pytest

from core.strategy.sensitivity import relative_ols


def nav_rows(returns):
    nav = 1_000.0
    rows = []
    for index, value in enumerate(returns):
        nav *= 1.0 + value
        rows.append({"trade_date": date(2020, 1, 1) + timedelta(days=index + 1), "reference_nav_cny": nav})
    return rows


def test_relative_ols_recovers_perfect_alpha_and_beta():
    benchmark_returns = [0.01, -0.02, 0.03, 0.005]
    strategy_returns = [0.001 + 2.0 * value for value in benchmark_returns]
    result = relative_ols(nav_rows(strategy_returns), nav_rows(benchmark_returns), 1_000)
    assert result["alpha_daily"] == pytest.approx(0.001, abs=1e-12)
    assert result["beta"] == pytest.approx(2.0, abs=1e-12)
    assert result["r_squared"] == pytest.approx(1.0, abs=1e-12)
    assert result["observations"] == 4


def test_relative_ols_rejects_degenerate_benchmark_returns():
    with pytest.raises(ValueError, match="degenerate"):
        relative_ols(nav_rows([0.01, 0.02, 0.03]), nav_rows([0.0, 0.0, 0.0]), 1_000)
