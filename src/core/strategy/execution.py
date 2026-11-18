"""Step 5 deterministic execution eligibility, capacity and cost rules."""
import hashlib
import math


def stable_id(prefix, *parts, length=24):
    value = "|".join(str(part) for part in parts)
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def execution_eligibility(fact, side):
    """Return the conservative D-day eligibility and its first blocking reason."""
    if fact.get("is_listed") is not True or fact.get("is_delisted") is True:
        return False, "not_listed_or_delisted"
    for field, reason in (("is_suspended", "suspended"),
                          ("is_full_day_suspended", "full_day_suspension"),
                          ("is_intraday_suspended", "intraday_suspension_conservative")):
        if fact.get(field) is not False:
            return False, reason
    if fact.get("quotation_exists") is not True:
        return False, "missing_quotation"
    if fact.get("limit_price_exists") is not True:
        return False, "missing_limit"
    open_price = fact.get("open")
    up_limit, down_limit = fact.get("up_limit"), fact.get("down_limit")
    if open_price is None or not math.isfinite(float(open_price)) or float(open_price) <= 0:
        return False, "invalid_open"
    if up_limit is None or down_limit is None:
        return False, "missing_limit"
    if not (float(down_limit) <= float(open_price) <= float(up_limit)):
        return False, "open_outside_valid_limits"
    if side == "BUY" and float(open_price) >= float(up_limit):
        return False, "buy_open_at_up_limit"
    if side == "SELL" and float(open_price) <= float(down_limit):
        return False, "sell_open_at_down_limit"
    return True, ""


def amount_capacity(order, fact, config):
    """Full-day D amount is a post-event upper bound, never an observed intraday fill."""
    amount = fact.get("amount")
    if amount is None or not math.isfinite(float(amount)) or float(amount) <= 0:
        return 0.0
    daily_cap = float(amount) * 1000.0 * float(config["execution"]["daily_amount_participation"])
    adv_cap = float(order["adv20_cny"]) * float(config["execution"]["daily_amount_participation"])
    return max(0.0, min(float(order["remaining_notional"]), daily_cap, adv_cap))


def quantity_rule(ts_code, config):
    rules = config["execution"]["quantity_constraint"]
    board = "star" if any(ts_code.startswith(prefix) for prefix in rules["star"]["prefixes"]) else "default"
    return rules[board]


def constrained_notional(notional, raw_open, adj_factor, ts_code, side, config, available_units=None):
    """Round raw-share equivalents, then convert back to R1 adjusted units via notional."""
    if notional <= 0:
        return 0.0, False
    rule = quantity_rule(ts_code, config)
    shares = float(notional) / float(raw_open)
    full_position_odd_lot = False
    if side == "SELL":
        if available_units is None:
            raise ValueError("Sell quantity constraint requires available adjusted units")
        available_shares = float(available_units) * float(adj_factor)
        tolerance = 1e-9 * max(1.0, available_shares)
        shares = min(shares, available_shares)
        if (config["execution"]["quantity_constraint"]["full_position_odd_lot_sell"]
                and shares >= available_shares - tolerance):
            constrained = available_shares
            full_position_odd_lot = True
        else:
            constrained = _floor_quantity(shares, rule["sell_min_shares"], rule["sell_increment_shares"])
    else:
        constrained = _floor_quantity(shares, rule["buy_min_shares"], rule["buy_increment_shares"])
    return constrained * float(raw_open), full_position_odd_lot


def _floor_quantity(quantity, minimum, increment):
    if quantity < float(minimum) - 1e-9:
        return 0.0
    rounded = math.floor((quantity + 1e-9) / float(increment)) * float(increment)
    return rounded if rounded >= float(minimum) else 0.0


class CommissionLedger:
    """Aggregate the minimum commission once per stock/side/trading day."""

    def __init__(self, config, scenario="main"):
        self.config = config
        self.scenario = scenario
        self.notional = {}
        self.paid = {}

    def preview(self, key, notional):
        rules = self.config["costs"][self.scenario]
        previous_n = self.notional.get(key, 0.0)
        previous_paid = self.paid.get(key, 0.0)
        total = max(float(rules["commission_minimum_cny_per_stock_direction_day"]),
                    (previous_n + notional) * float(rules["commission_rate"]))
        return max(0.0, total - previous_paid)

    def charge(self, key, notional):
        commission = self.preview(key, notional)
        self.notional[key] = self.notional.get(key, 0.0) + notional
        self.paid[key] = self.paid.get(key, 0.0) + commission
        return commission


def cost_components(notional, side, volatility, adv20, config, commission, scenario="main"):
    rules = config["costs"][scenario]
    participation = notional / adv20 if adv20 > 0 else math.inf
    transfer = notional * float(rules["transfer_fee_rate"])
    tax = notional * float(rules["sell_tax_rate"]) if side == "SELL" else 0.0
    impact = notional * (float(rules["impact_fixed_rate"]) +
                         float(rules["impact_volatility_coefficient"]) * volatility * math.sqrt(participation))
    return {"commission": commission, "transfer": transfer, "tax": tax, "impact": impact,
            "total": commission + transfer + tax + impact}


def affordable_notional(requested, available_cash, side, volatility, adv20, config,
                        commission_ledger, commission_key):
    """Deterministic bisection for the cash-inclusive fill cap; no cash rounding is invented."""
    if requested <= 0:
        return 0.0
    def total(value):
        commission = commission_ledger.preview(commission_key, value)
        return value + cost_components(value, side, volatility, adv20, config, commission)["total"]
    if total(requested) <= available_cash:
        return requested
    low, high = 0.0, requested
    for _ in range(64):
        middle = (low + high) / 2
        if total(middle) <= available_cash:
            low = middle
        else:
            high = middle
    return low
