from datetime import date

import pytest

from core.strategy.benchmark import IdealTargetAccount
from core.strategy.promotion import boundary_check, promotion_gate
from core.strategy.research import segment_definitions


THRESHOLDS = {"minimum_annualized_return": "0.12", "maximum_drawdown_abs": "0.15",
              "minimum_information_ratio": "0.8", "maximum_stale_weight": "0.01",
              "maximum_non_tradable_weight": "0.01"}


def evidence(period, annualized, drawdown, ir, stale=0.0, non_tradable=0.0):
    performance = [{"period_id": period, "account_id": "c0_three_module_fixed__executed_proxy_net",
                    "annualized_return": annualized, "max_drawdown": drawdown}]
    relative = [{"period_id": period, "benchmark_id": "eligible_passive_net",
                 "annualized_information_ratio": ir}]
    exposures = [{"stale_weight": stale, "non_tradable_weight": non_tradable}]
    return performance, relative, exposures


def test_design_failure_cannot_be_overwritten_by_validation_improvement():
    design = boundary_check(*evidence("design", 0.09, -0.20, -0.2), "design", THRESHOLDS)
    validation = boundary_check(*evidence("validation", 0.20, -0.10, 1.2), "validation", THRESHOLDS)
    gate = promotion_gate(design, validation)
    assert design["passed"] is False
    assert validation["passed"] is True
    assert gate["research_verification_passed"] is False
    assert gate["promotion_status"] == "no_champion_no_promotion"
    assert gate["economic_promoted"] is False


def test_stale_positions_and_confirmation_period_boundary_are_explicit():
    checked = boundary_check(*evidence("validation", 0.20, -0.10, 1.2, stale=0.02),
                             "validation", THRESHOLDS)
    assert checked["passed"] is False
    assert "stale_weight_max" in checked["failures"]
    segments = segment_definitions(date(2022, 1, 1), date(2023, 12, 31), "validation")
    assert segments[0][0] == "validation"
    assert max(item[2] for item in segments) == date(2023, 12, 31)


def test_validation_warmup_preserves_last_quote_for_suspended_position():
    d1, d2 = date(2021, 12, 31), date(2022, 1, 4)
    targets = {d1: {"SA001.SZ": 0.95}, d2: {"SA001.SZ": 0.95}}
    facts = {d1: {"open": 10.0, "close": 10.0, "adj_factor": 1.0},
             d2: {"open": None, "close": None, "adj_factor": None}}
    provider = lambda day, codes: {code: facts[day] for code in codes}
    warm = IdealTargetAccount(1_000, [d1, d2], provider, targets).run(d2)
    assert warm[-1]["reference_nav_cny"] == pytest.approx(1_000)
    cold = IdealTargetAccount(1_000, [d2], provider, {d2: targets[d2]})
    with pytest.raises(ValueError, match="no valuation price"):
        cold.run(d2)
