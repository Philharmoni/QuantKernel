"""Step 7 deterministic continuous-NAV research metrics."""
import math


def quantile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def extended_performance(rows, initial_nav, annualization_days=252):
    ordered = sorted(rows, key=lambda row: row["trade_date"])
    if not ordered or initial_nav <= 0:
        return {"observations": 0, "cumulative_return": None}
    navs = [float(initial_nav)] + [float(row["reference_nav_cny"]) for row in ordered]
    returns = [navs[index] / navs[index - 1] - 1.0 for index in range(1, len(navs))]
    count = len(returns)
    mean = sum(returns) / count
    variance = sum((value - mean) ** 2 for value in returns) / count
    cumulative = navs[-1] / navs[0] - 1.0
    annualized = (navs[-1] / navs[0]) ** (annualization_days / count) - 1.0
    downside = math.sqrt(sum(min(value, 0.0) ** 2 for value in returns) / count * annualization_days)
    var95 = quantile(returns, 0.05)
    tail = [value for value in returns if value <= var95]
    peak, peak_date = navs[0], None
    worst, worst_start, worst_end = 0.0, None, None
    recovery = None
    for index, value in enumerate(navs[1:]):
        day = ordered[index]["trade_date"]
        if value > peak:
            peak, peak_date = value, day
        drawdown = value / peak - 1.0
        if drawdown < worst:
            worst, worst_start, worst_end = drawdown, peak_date, day
            recovery = None
        if worst_start is not None and day > worst_end and value >= peak and recovery is None:
            recovery = day
    final_peak = max(navs)
    longest_unrecovered = 0
    for index in range(len(navs) - 1, -1, -1):
        if navs[index] == final_peak:
            longest_unrecovered = len(navs) - 1 - index
            break
    return {"observations": count, "cumulative_return": cumulative, "annualized_return": annualized,
        "annualized_volatility": math.sqrt(variance * annualization_days),
        "annualized_downside_volatility": downside,
        "sortino": mean * annualization_days / downside if downside > 0 else None,
        "max_drawdown": worst, "max_drawdown_start": worst_start, "max_drawdown_end": worst_end,
        "max_drawdown_recovery": recovery, "longest_unrecovered_days": longest_unrecovered,
        "calmar": annualized / abs(worst) if worst < 0 else None,
        "historical_var_95": var95, "historical_es_95": sum(tail) / len(tail) if tail else None,
        "worst_day": min(returns), "daily_mean": mean}


def monthly_returns(rows, initial_nav):
    ordered = sorted(rows, key=lambda row: row["trade_date"])
    groups = {}
    for row in ordered:
        key = f"{row['trade_date'].year:04d}-{row['trade_date'].month:02d}"
        groups.setdefault(key, []).append(row)
    result = []
    previous = float(initial_nav)
    for period, items in sorted(groups.items()):
        nav = float(items[-1]["reference_nav_cny"])
        result.append({"period_id": period, "start_nav": previous, "end_nav": nav,
                       "return": nav / previous - 1.0, "observations": len(items)})
        previous = nav
    return result


def segment_definitions(design_start, design_end, period_id="design"):
    years = list(range(design_start.year, design_end.year + 1))
    segments = [(period_id, design_start, design_end)]
    segments.extend((f"year:{year}", max(design_start, design_start.replace(year=year, month=1, day=1)),
                     min(design_end, design_start.replace(year=year, month=12, day=31))) for year in years)
    if len(years) >= 2:
        segments.append((f"window:{years[0]}-{years[1]}", design_start,
                         min(design_end, design_start.replace(year=years[1], month=12, day=31))))
    return segments
