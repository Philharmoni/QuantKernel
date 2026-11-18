"""Step 12 deterministic R1 sensitivity and relative OLS helpers."""
import math


def relative_ols(strategy_rows, benchmark_rows, initial_capital, annualization_days=252):
    left = {row["trade_date"]: float(row["reference_nav_cny"]) for row in strategy_rows}
    right = {row["trade_date"]: float(row["reference_nav_cny"]) for row in benchmark_rows}
    dates = sorted(set(left) & set(right))
    if len(dates) < 3:
        raise ValueError("Relative OLS requires at least three common NAV dates")
    strategy_nav = [initial_capital] + [left[day] for day in dates]
    benchmark_nav = [initial_capital] + [right[day] for day in dates]
    y = [strategy_nav[index] / strategy_nav[index - 1] - 1.0 for index in range(1, len(strategy_nav))]
    x = [benchmark_nav[index] / benchmark_nav[index - 1] - 1.0 for index in range(1, len(benchmark_nav))]
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    variance_x = sum((value - mean_x) ** 2 for value in x)
    if variance_x <= 0:
        raise ValueError("Relative OLS benchmark returns are degenerate")
    beta = sum((x[index] - mean_x) * (y[index] - mean_y) for index in range(len(x))) / variance_x
    alpha = mean_y - beta * mean_x
    residuals = [y[index] - alpha - beta * x[index] for index in range(len(x))]
    variance_y = sum((value - mean_y) ** 2 for value in y)
    residual_ss = sum(value * value for value in residuals)
    r_squared = 1.0 - residual_ss / variance_y if variance_y > 0 else None
    return {"observations": len(x), "alpha_daily": alpha,
            "alpha_annualized": alpha * annualization_days, "beta": beta, "r_squared": r_squared,
            "residual_daily_volatility": math.sqrt(residual_ss / len(x)),
            "strategy_mean_daily_return": mean_y, "benchmark_mean_daily_return": mean_x}
