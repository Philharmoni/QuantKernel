"""Step 6 passive targets, ideal accounts and small-window performance."""
import math

from core.strategy.execution import stable_id


def market_value_targets(rows, stock_weight):
    total = sum(float(row["circ_mv"]) for row in rows)
    if total <= 0 or not math.isfinite(total):
        raise ValueError("Passive reference has invalid total circ_mv")
    return {row["ts_code"]: stock_weight * float(row["circ_mv"]) / total for row in rows}


def passive_orders(targets_by_date, infos_by_date, initial_capital, benchmark_id):
    orders = []
    previous = {}
    for signal_date, execution_date in sorted(targets_by_date):
        targets = targets_by_date[(signal_date, execution_date)]
        infos = infos_by_date[(signal_date, execution_date)]
        for code in sorted(set(previous) | set(targets)):
            delta = targets.get(code, 0.0) - previous.get(code, 0.0)
            notional = abs(delta) * initial_capital
            if notional <= 1e-8:
                continue
            info = infos.get(code, {})
            orders.append({"order_id": stable_id("passive_", benchmark_id, signal_date, code,
                "BUY" if delta > 0 else "SELL"), "signal_date": signal_date,
                "planned_execution_date": execution_date, "ts_code": code,
                "side": "BUY" if delta > 0 else "SELL", "purpose": "rebalance",
                "reference_notional_cny": notional, "adv20_cny": info.get("adv20_cny"),
                "return_volatility_20": info.get("return_volatility_20"),
                "order_status": "intent", "time_in_force": "DAY"})
        previous = dict(targets)
    return orders


class IdealTargetAccount:
    """Ideal D-open target transitions without execution constraints or costs."""
    def __init__(self, initial_capital, calendar, facts_provider, targets_by_execution):
        self.initial_capital = float(initial_capital)
        self.calendar = list(calendar)
        self.facts_provider = facts_provider
        self.targets = targets_by_execution

    @staticmethod
    def _price(fact, mark, field):
        raw, adj = fact.get(field), fact.get("adj_factor")
        if raw is not None and adj is not None and float(raw) > 0 and float(adj) > 0:
            return float(raw) * float(adj), float(raw), float(adj)
        if mark is None:
            raise ValueError("Ideal account has no valuation price")
        return mark[0] * mark[1], mark[0], mark[1]

    def run(self, end_date):
        if not self.targets:
            raise ValueError("Ideal account requires targets")
        first = min(self.targets)
        dates = [day for day in self.calendar if first <= day <= end_date]
        cash, units, marks = self.initial_capital, {}, {}
        rows = []
        for day in dates:
            target = self.targets.get(day)
            codes = set(units) | set(target or {})
            facts = self.facts_provider(day, codes)
            if target is not None:
                pretrade = cash
                for code, amount in units.items():
                    price, raw, adj = self._price(facts.get(code, {}), marks.get(code), "open")
                    pretrade += amount * price
                    marks[code] = (raw, adj)
                units = {}
                for code, weight in sorted(target.items()):
                    if weight <= 1e-12:
                        continue
                    price, raw, adj = self._price(facts.get(code, {}), marks.get(code), "open")
                    units[code] = weight * pretrade / price
                    marks[code] = (raw, adj)
                cash = (1.0 - sum(target.values())) * pretrade
            position = 0.0
            for code, amount in units.items():
                price, raw, adj = self._price(facts.get(code, {}), marks.get(code), "close")
                marks[code] = (raw, adj)
                position += amount * price
            nav = cash + position
            rows.append({"trade_date": day, "total_cash": cash,
                "reference_position_value_cny": position, "stress_position_value_cny": position,
                "reference_nav_cny": nav, "stress_nav_cny": nav})
        return rows


def performance_metrics(rows, initial_capital, annualization_days=252):
    ordered = sorted(rows, key=lambda row: row["trade_date"])
    if not ordered:
        return {"observations": 0, "cumulative_return": None, "annualized_return": None,
                "annualized_volatility": None, "max_drawdown": None}
    navs = [float(initial_capital)] + [float(row["reference_nav_cny"]) for row in ordered]
    returns = [navs[index] / navs[index - 1] - 1.0 for index in range(1, len(navs))]
    cumulative = navs[-1] / navs[0] - 1.0
    annualized = (navs[-1] / navs[0]) ** (annualization_days / len(returns)) - 1.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    peak, max_drawdown = navs[0], 0.0
    for value in navs:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0)
    return {"observations": len(returns), "cumulative_return": cumulative,
            "annualized_return": annualized, "annualized_volatility": math.sqrt(variance * annualization_days),
            "max_drawdown": max_drawdown}


def relative_metrics(strategy_rows, benchmark_rows, annualization_days=252):
    left = {row["trade_date"]: float(row["reference_nav_cny"]) for row in strategy_rows}
    right = {row["trade_date"]: float(row["reference_nav_cny"]) for row in benchmark_rows}
    dates = sorted(set(left) & set(right))
    if not dates:
        return {"relative_nav_growth": None, "annualized_information_ratio": None}
    active = []
    for previous, day in zip([None] + dates[:-1], dates):
        if previous is None:
            continue
        active.append(left[day] / left[previous] - right[day] / right[previous])
    ir = None
    if active:
        mean = sum(active) / len(active)
        variance = sum((value - mean) ** 2 for value in active) / len(active)
        ir = math.sqrt(annualization_days) * mean / math.sqrt(variance) if variance > 0 else None
    return {"relative_nav_growth": left[dates[-1]] / right[dates[-1]] - 1.0,
            "annualized_information_ratio": ir}
