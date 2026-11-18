from core.strategy.reporting_build import Step9Builder, number, pct


def sample_metrics():
    accounts = ["c0_three_module_fixed__ideal_target_gross", "c0_three_module_fixed__executed_proxy_gross",
                "c0_three_module_fixed__executed_proxy_net", "eligible_passive__executed_proxy_net",
                "common_support_passive__executed_proxy_net"]
    metrics = {"design": {}, "validation": {}, "relative": {"design": [], "validation": []}}
    for period, value in (("design", 0.2456328829), ("validation", -0.05015192195)):
        for account in accounts:
            metrics[period][account] = {"cumulative_return": value, "annualized_return": value / 2,
                "annualized_volatility": 0.19, "max_drawdown": -0.20, "sortino": 0.8, "calmar": 0.4}
        metrics["relative"][period] = [{"benchmark_id": "eligible_passive_net",
            "relative_nav_growth": -0.07 if period == "design" else 0.18, "annualized_information_ratio": -0.2}]
    return metrics


def test_performance_report_preserves_negative_validation_and_no_champion_decision():
    builder = object.__new__(Step9Builder)
    report = builder.performance_report(sample_metrics())
    assert "24.5633%" in report
    assert "-5.0152%" in report
    assert "no_champion_no_promotion" in report
    assert "未切换候选或调参" in report


def test_report_formatting_and_risk_summary_are_deterministic():
    assert pct(-0.05015192195) == "-5.0152%"
    assert number(1.17448966087) == "1.1745"
    rows = [{"stale_weight": 0.01, "non_tradable_weight": 0.0, "cash_weight": 0.05, "top1_weight": 0.02},
            {"stale_weight": 0.02, "non_tradable_weight": 0.01, "cash_weight": 0.08, "top1_weight": 0.03}]
    summary = Step9Builder.risk_summary(rows)
    assert summary == {"max_stale_weight": 0.02, "max_non_tradable_weight": 0.01,
                       "max_cash_weight": 0.08, "max_top1_weight": 0.03}
