"""Single-factor evaluation formulas (plan Step 7), shared by tests and builds.

Conventions frozen in configs/factor_research.yaml:
- Pearson IC on valid (factor,label) pairs; RankIC is Pearson on average ranks.
- Displayed IC standard deviations use ddof=0; inference uses ddof=1 for the
  plain h=1 t-stat and Bartlett-weighted Newey-West autocovariances (lag h-1)
  for overlapping h>1 labels.
- Quantiles sort by expected_direction-adjusted factor with average ranks;
  group 1 is always the expected-best group. Monotonicity is the negative
  Spearman correlation between group number and group return (ideal = +1).
- Performance uses non-overlapping period returns, population std (ddof=0),
  annualized wealth return (not mean*periods), drawdown on the wealth path,
  and no transaction costs. One-way group turnover is
  1 - |current members intersect previous members| / |previous members|.
"""
import math

import numpy as np


def average_rank(values) -> np.ndarray:
    """One-based average ranks in ascending order, NaNs excluded by caller."""
    values = np.asarray(values, dtype=np.float64)
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(len(values), dtype=np.float64)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and sorted_values[end] == sorted_values[start]:
            end += 1
        ranks[order[start:end]] = (start + 1 + end) / 2.0
        start = end
    return ranks


def pearson(x, y) -> float | None:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if len(x) < 2:
        return None
    dx = x - np.mean(x)
    dy = y - np.mean(y)
    denominator = math.sqrt(float(np.sum(dx * dx) * np.sum(dy * dy)))
    if denominator == 0.0:
        return None
    return float(np.sum(dx * dy) / denominator)


def rank_ic(x, y) -> float | None:
    return pearson(average_rank(x), average_rank(y))


def plain_t_stat(series) -> float | None:
    series = np.asarray(series, dtype=np.float64)
    n = len(series)
    if n < 2:
        return None
    std = float(np.std(series, ddof=1))
    if std == 0.0:
        return None
    return float(np.mean(series) / (std / math.sqrt(n)))


def newey_west_t(series, horizon) -> float | None:
    """Mean t-stat with Bartlett NW lag h-1; h=1 uses the plain t-stat."""
    series = np.asarray(series, dtype=np.float64)
    n = len(series)
    if horizon == 1:
        return plain_t_stat(series)
    if n < 2:
        return None
    centered = series - np.mean(series)
    gamma0 = float(np.sum(centered * centered) / n)
    long_run_variance = gamma0
    for lag in range(1, horizon):
        gamma = float(np.sum(centered[lag:] * centered[:-lag]) / n)
        long_run_variance += 2.0 * (1.0 - lag / horizon) * gamma
    if long_run_variance <= 0.0:
        return None
    return float(np.mean(series) / math.sqrt(long_run_variance / n))


def quantile_group_ids(values, expected_direction, n_groups) -> np.ndarray:
    """Average-rank quantiles; group 1 is the expected-best group."""
    values = np.asarray(values, dtype=np.float64)
    ranks = average_rank(values)
    ascending_group = np.floor((ranks - 1.0) * n_groups / len(values)).astype(int) + 1
    ascending_group = np.minimum(ascending_group, n_groups)
    if expected_direction == 1:
        return n_groups + 1 - ascending_group
    return ascending_group


def one_way_turnover(previous_members, current_members) -> float | None:
    previous_members = set(previous_members)
    current_members = set(current_members)
    if not previous_members:
        return None
    return 1.0 - len(previous_members & current_members) / len(previous_members)


def performance_metrics(returns, dates, periods_per_year) -> dict:
    """Cost-free group performance over an ordered non-overlapping series."""
    returns = np.asarray(returns, dtype=np.float64)
    result = {"n_periods": len(returns), "cumulative_return": None,
              "annualized_return": None, "mean_period_return": None,
              "std_period_return": None, "sharpe": None, "max_drawdown": None,
              "max_drawdown_start": None, "max_drawdown_end": None,
              "calmar": None, "win_rate": None, "profit_loss_ratio": None}
    if len(returns) == 0:
        return result
    wealth = np.cumprod(1.0 + returns)
    cumulative = float(wealth[-1] - 1.0)
    annualized = float(wealth[-1] ** (periods_per_year / len(returns)) - 1.0) if wealth[-1] > 0 else None
    mean = float(np.mean(returns))
    std = float(np.std(returns))  # ddof=0, matching the golden formula fixture
    peaks = np.maximum.accumulate(np.r_[1.0, wealth])
    drawdowns = np.r_[1.0, wealth] / peaks - 1.0
    end = int(np.argmin(drawdowns))
    start = int(np.argmax(np.r_[1.0, wealth][:end + 1]))
    max_drawdown = float(drawdowns[end])
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    result.update({
        "cumulative_return": cumulative,
        "annualized_return": annualized,
        "mean_period_return": mean,
        "std_period_return": std,
        "sharpe": mean / std * math.sqrt(periods_per_year) if std > 0 else None,
        "max_drawdown": max_drawdown,
        "max_drawdown_start": str(dates[max(start - 1, 0)]),
        "max_drawdown_end": str(dates[max(end - 1, 0)]),
        "calmar": annualized / abs(max_drawdown) if annualized is not None and max_drawdown < 0 else None,
        "win_rate": float(len(wins) / len(returns)),
        "profit_loss_ratio": (float(np.mean(wins)) / abs(float(np.mean(losses)))
                              if len(wins) and len(losses) else None),
    })
    return result


def summarize_ic(daily_ics, daily_rank_ics, horizon, annualization_days=252) -> dict:
    ics = np.asarray([x for x in daily_ics if x is not None and math.isfinite(x)], dtype=np.float64)
    ranks = np.asarray([x for x in daily_rank_ics if x is not None and math.isfinite(x)], dtype=np.float64)

    def stats(values):
        if len(values) == 0:
            return {"n_days": 0, "mean": None, "std": None, "win_rate": None}
        return {"n_days": len(values), "mean": float(np.mean(values)),
                "std": float(np.std(values)),
                "win_rate": float(np.mean(values > 0))}

    result = {"ic": stats(ics), "rank_ic": stats(ranks)}
    rank_mean = result["rank_ic"]["mean"]
    rank_std = result["rank_ic"]["std"]
    icir = rank_mean / rank_std if rank_mean is not None and rank_std and rank_std > 0 else None
    result.update(rank_icir=icir,
                  annualized_rank_icir=icir * math.sqrt(annualization_days) if icir is not None else None,
                  rank_ic_t_stat=newey_west_t(ranks, horizon))
    return result
