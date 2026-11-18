"""Deterministic daily Step 5 execution and R1 account state loop."""
from core.strategy.accounting import R1Account
from core.strategy.execution import (CommissionLedger, affordable_notional, amount_capacity,
                                     constrained_notional, cost_components, execution_eligibility,
                                     quantity_rule, stable_id)


class R1Engine:
    def __init__(self, config, calendar, facts_provider, orders, scores=None, risk_templates=None, nav_only=False):
        self.config = config
        self.calendar = list(calendar)
        self.facts_provider = facts_provider
        self.orders = sorted((dict(order) for order in orders if order.get("order_status", "intent") == "intent"),
                             key=lambda order: (order["planned_execution_date"], order["order_id"]))
        self.scores = scores or {}
        self.risk_templates = risk_templates
        self.nav_only = nav_only

    def run(self, end_date=None):
        if not self.orders:
            raise ValueError("Step 5 requires at least one executable order intent")
        first = min(order["planned_execution_date"] for order in self.orders)
        last = self.calendar[-1] if end_date is None else min(self.calendar[-1], end_date)
        dates = [day for day in self.calendar if first <= day <= last]
        if not dates:
            raise ValueError("No authorized execution dates for Step 5")
        next_dates = {day: self.calendar[index + 1] if index + 1 < len(self.calendar) else None
                      for index, day in enumerate(self.calendar)}
        account = R1Account(self.config)
        commissions = CommissionLedger(self.config)
        tables = {name: [] for name in ("execution_eligibility", "order_event", "fill", "position_lot",
                                        "cash_ledger", "nav_daily", "cost_daily", "risk_daily")}
        risk_pending = set()
        risk_detected = {}
        templates = self.risk_templates or {order["ts_code"]: order for order in self.orders
                                           if order["purpose"] == "risk_exit"}
        tables["cash_ledger"].append({"cash_event_id": stable_id("cash_", "initial", dates[0]),
            "trade_date": dates[0], "event_type": "initial_capital", "source_id": None,
            "available_delta": account.available_cash, "sale_restricted_delta": 0.0, "other_frozen_delta": 0.0,
            "available_balance": account.available_cash, "sale_restricted_balance": 0.0,
            "other_frozen_balance": 0.0, "release_date": None, "accounting_mode": "adjusted_unit_proxy"})
        previous_nav = float(self.config["objective"]["initial_capital_cny"])
        for day in dates:
            released = account.release_sale_cash()
            if released:
                tables["cash_ledger"].append(self._cash_event(day, "sale_cash_release", None, account,
                                                              available_delta=released, sale_delta=-released))
            scheduled = [order for order in self.orders if order["planned_execution_date"] == day]
            for order in scheduled:
                order["requested_notional"] = float(order["reference_notional_cny"])
                order["remaining_notional"] = float(order["reference_notional_cny"])
                if order["purpose"] == "risk_exit":
                    risk_pending.add(order["ts_code"])
                    risk_detected.setdefault(order["ts_code"], order["signal_date"])
            working = list(scheduled)
            scheduled_risk_codes = {order["ts_code"] for order in scheduled if order["purpose"] == "risk_exit"}
            for code in sorted(risk_pending - scheduled_risk_codes):
                if account.position_units(code) <= 1e-12:
                    risk_pending.discard(code)
                    continue
                lots = [lot for lot in account.lots.get(code, []) if lot["units"] > 1e-12]
                if lots and all(lot["non_tradable"] for lot in lots):
                    continue
                template = templates.get(code, {})
                notional = sum(lot["units"] * lot["last_raw_price"] * lot["last_adj_factor"] for lot in lots)
                generated = {"order_id": stable_id("risk_", code, day), "signal_date": risk_detected.get(code, day),
                    "planned_execution_date": day, "ts_code": code, "side": "SELL", "purpose": "risk_exit",
                    "reference_notional_cny": notional, "requested_notional": notional, "remaining_notional": notional,
                    "adv20_cny": template.get("adv20_cny"), "return_volatility_20": template.get("return_volatility_20"),
                    "order_status": "intent", "time_in_force": "DAY"}
                working.append(generated)
            codes = {order["ts_code"] for order in working} | {code for code, lots in account.lots.items()
                                                               if any(lot["units"] > 1e-12 for lot in lots)}
            facts = self.facts_provider(day, codes)
            for code in sorted({order["ts_code"] for order in working}):
                fact = facts.get(code, {})
                buy_ok, buy_reason = execution_eligibility(fact, "BUY")
                sell_ok, sell_reason = execution_eligibility(fact, "SELL")
                tables["execution_eligibility"].append({"execution_date": day, "ts_code": code,
                    "open": fact.get("open"), "up_limit": fact.get("up_limit"), "down_limit": fact.get("down_limit"),
                    "close": fact.get("close"), "adj_factor": fact.get("adj_factor"),
                    "full_day_amount_cny": float(fact["amount"]) * 1000 if fact.get("amount") is not None else None,
                    "buy_eligible": buy_ok, "buy_block_reason": buy_reason,
                    "sell_eligible": sell_ok, "sell_block_reason": sell_reason,
                    "execution_model": "daily_open_proxy_v1"})
            working.sort(key=lambda order: (0 if order["purpose"] == "risk_exit" else 1,
                -self.scores.get((order.get("signal_date"), order["ts_code"]), float("-inf")), order["ts_code"], order["order_id"]))
            day_fills = []
            for order in working:
                self._execute_order(order, day, next_dates[day], facts.get(order["ts_code"], {}),
                                    account, commissions, tables, day_fills)
            valuation = account.mark_and_value(day, facts)
            tables["position_lot"].extend(valuation.pop("position_lot"))
            nav = account.total_cash + valuation["reference_position_value_cny"]
            stress_nav = account.total_cash + valuation["stress_position_value_cny"]
            tables["nav_daily"].append({"trade_date": day, "account_id": "r1_main", "scenario_id": "main",
                "available_cash": account.available_cash, "sale_restricted_cash": account.sale_restricted_cash,
                "other_frozen_cash": account.other_frozen_cash, "total_cash": account.total_cash,
                **valuation, "reference_nav_cny": nav, "stress_nav_cny": stress_nav,
                "stale_weight": valuation["stale_position_value_cny"] / nav if nav else 0.0,
                "non_tradable_weight": valuation["non_tradable_position_value_cny"] / nav if nav else 0.0,
                "accounting_mode": "adjusted_unit_proxy", "real_share_account_ready": False})
            buy = sum(fill["notional_cny"] for fill in day_fills if fill["side"] == "BUY")
            sell = sum(fill["notional_cny"] for fill in day_fills if fill["side"] == "SELL")
            costs = {name: sum(fill[name + "_cny"] for fill in day_fills)
                     for name in ("commission", "transfer", "tax", "impact", "total_cost")}
            tables["cost_daily"].append({"trade_date": day, "account_id": "r1_main", "scenario_id": "main",
                "buy_notional_cny": buy, "sell_notional_cny": sell, **costs,
                "two_sided_turnover": (buy + sell) / previous_nav if previous_nav else 0.0,
                "one_way_turnover": (buy + sell) / (2 * previous_nav) if previous_nav else 0.0,
                "turnover_denominator_nav_cny": previous_nav})
            for code in list(account.lots):
                if account.position_units(code) <= 1e-12:
                    continue
                fact = facts.get(code, {})
                if fact.get("is_st") is True or fact.get("is_delisted") is True or fact.get("is_listed") is not True:
                    risk_pending.add(code)
                    risk_detected.setdefault(code, day)
            tables["risk_daily"].append({"trade_date": day, "account_id": "r1_main", "scenario_id": "main",
                "negative_cash_count": account.negative_cash_count, "oversell_count": account.oversell_count,
                "t_plus_one_violation_count": account.t_plus_one_violation_count,
                "open_position_count": sum(account.position_units(code) > 1e-12 for code in account.lots),
                "risk_pending_count": len(risk_pending),
                "stale_position_count": sum(lot["stale_days"] > 0 for lots in account.lots.values() for lot in lots if lot["units"] > 1e-12),
                "non_tradable_position_count": sum(lot["non_tradable"] for lots in account.lots.values() for lot in lots if lot["units"] > 1e-12),
                "r2_blocked": True})
            if self.nav_only:
                for name in ("execution_eligibility", "order_event", "fill", "position_lot", "cash_ledger"):
                    tables[name].clear()
            previous_nav = nav
        return tables

    def _execute_order(self, order, day, next_day, fact, account, commissions, tables, day_fills):
        events = tables["order_event"]
        requested = order["remaining_notional"]
        events.append(self._order_event(order, 1, "submitted", requested, 0.0, requested, ""))
        reason = ""
        if order.get("return_volatility_20") is None or order.get("adv20_cny") is None:
            reason = "missing_adv_or_volatility"
        else:
            eligible, reason = execution_eligibility(fact, order["side"])
        notional = 0.0
        if not reason:
            notional = amount_capacity(order, fact, self.config)
            if notional <= 0:
                reason = "no_daily_liquidity"
        if not reason and order["side"] == "SELL":
            price = float(fact["open"]) * float(fact["adj_factor"])
            notional = min(notional, account.sellable_units(order["ts_code"], day) * price)
            if notional <= 0:
                reason = "t_plus_one_or_no_sellable_position"
        commission_key = (day, order["ts_code"], order["side"])
        if not reason and order["side"] == "BUY":
            notional = affordable_notional(notional, account.available_cash, order["side"],
                float(order["return_volatility_20"]), float(order["adv20_cny"]), self.config, commissions, commission_key)
            if notional <= 0:
                reason = "insufficient_available_cash"
        full_position_odd_lot = False
        if not reason:
            available_units = account.sellable_units(order["ts_code"], day) if order["side"] == "SELL" else None
            notional, full_position_odd_lot = constrained_notional(notional, float(fact["open"]),
                float(fact["adj_factor"]), order["ts_code"], order["side"], self.config, available_units)
            if notional <= 0:
                reason = "below_minimum_or_invalid_quantity"
        if not reason and order["side"] == "SELL":
            preview = cost_components(notional, order["side"], float(order["return_volatility_20"]),
                float(order["adv20_cny"]), self.config, commissions.preview(commission_key, notional))
            if account.available_cash + min(0.0, notional - preview["total"]) < -1e-7:
                reason = "insufficient_cash_for_sell_fees"
        if reason:
            events.append(self._order_event(order, 2, "rejected", requested, 0.0, requested, reason))
            return
        commission = commissions.charge(commission_key, notional)
        costs = cost_components(notional, order["side"], float(order["return_volatility_20"]),
                                float(order["adv20_cny"]), self.config, commission)
        fill = {"fill_id": stable_id("fill_", order["order_id"], day), "order_id": order["order_id"],
            "execution_date": day, "ts_code": order["ts_code"], "side": order["side"], "purpose": order["purpose"],
            "raw_open": float(fact["open"]), "adj_factor": float(fact["adj_factor"]), "notional_cny": notional,
            "units": notional / (float(fact["open"]) * float(fact["adj_factor"])),
            "share_equivalent": notional / float(fact["open"]),
            "quantity_rule_id": quantity_rule(order["ts_code"], self.config)["rule_id"],
            "full_position_odd_lot": full_position_odd_lot,
            "commission_cny": costs["commission"], "transfer_cny": costs["transfer"], "tax_cny": costs["tax"],
            "impact_cny": costs["impact"], "total_cost_cny": costs["total"],
            "fill_model": "proxy_not_observed", "liquidity_basis": "full_day_upper_bound",
            "accounting_mode": "adjusted_unit_proxy"}
        account.apply_fill(fill, next_day)
        tables["fill"].append(fill)
        day_fills.append(fill)
        remaining = max(0.0, requested - notional)
        order["remaining_notional"] = remaining
        net_proceeds = notional - costs["total"]
        available_delta = -(notional + costs["total"]) if order["side"] == "BUY" else min(0.0, net_proceeds)
        sale_delta = max(0.0, net_proceeds) if order["side"] == "SELL" else 0.0
        tables["cash_ledger"].append(self._cash_event(day, "fill", fill["fill_id"], account,
            available_delta=available_delta, sale_delta=sale_delta,
            release_date=next_day if order["side"] == "SELL" and sale_delta > 0 else None))
        events.append(self._order_event(order, 2, "filled" if remaining <= 1e-8 else "partially_filled",
                                        requested, notional, remaining, ""))
        if remaining > 1e-8:
            events.append(self._order_event(order, 3, "expired", requested, notional, remaining, "day_order_expired"))

    @staticmethod
    def _order_event(order, sequence, event_type, requested, filled, remaining, reason):
        return {"order_id": order["order_id"], "event_seq": sequence, "event_date": order["planned_execution_date"],
            "event_type": event_type, "ts_code": order["ts_code"], "side": order["side"], "purpose": order["purpose"],
            "requested_notional_cny": requested, "filled_notional_cny": filled,
            "remaining_notional_cny": remaining, "reason": reason, "execution_model": "daily_open_proxy_v1"}

    @staticmethod
    def _cash_event(day, event_type, source_id, account, available_delta=0.0, sale_delta=0.0, release_date=None):
        return {"cash_event_id": stable_id("cash_", day, event_type, source_id), "trade_date": day,
            "event_type": event_type, "source_id": source_id, "available_delta": available_delta,
            "sale_restricted_delta": sale_delta, "other_frozen_delta": 0.0,
            "available_balance": account.available_cash, "sale_restricted_balance": account.sale_restricted_cash,
            "other_frozen_balance": account.other_frozen_cash, "release_date": release_date,
            "accounting_mode": "adjusted_unit_proxy"}
