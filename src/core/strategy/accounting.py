"""Step 5 R1 adjusted-unit proxy positions, cash and valuation."""
import math


class R1Account:
    def __init__(self, config):
        if config["accounting"]["mode"] != "adjusted_unit_proxy":
            raise PermissionError("Step 5 only permits the frozen R1 adjusted-unit proxy")
        self.config = config
        self.available_cash = float(config["objective"]["initial_capital_cny"])
        self.sale_restricted_cash = 0.0
        self.other_frozen_cash = 0.0
        self.lots = {}
        self.oversell_count = 0
        self.t_plus_one_violation_count = 0
        self.negative_cash_count = 0

    @property
    def total_cash(self):
        return self.available_cash + self.sale_restricted_cash + self.other_frozen_cash

    def release_sale_cash(self):
        amount = self.sale_restricted_cash
        self.available_cash += amount
        self.sale_restricted_cash = 0.0
        return amount

    def sellable_units(self, code, day):
        return sum(lot["units"] for lot in self.lots.get(code, [])
                   if lot["sellable_date"] is not None and lot["sellable_date"] <= day and not lot["non_tradable"])

    def position_units(self, code):
        return sum(lot["units"] for lot in self.lots.get(code, []))

    def apply_fill(self, fill, next_trade_date):
        code, side = fill["ts_code"], fill["side"]
        units, notional, fees = fill["units"], fill["notional_cny"], fill["total_cost_cny"]
        if side == "BUY":
            self.available_cash -= notional + fees
            if self.available_cash < 0 and abs(self.available_cash) < 1e-7:
                self.available_cash = 0.0
            lot = {"ts_code": code, "lot_id": fill["fill_id"], "buy_date": fill["execution_date"],
                   "sellable_date": next_trade_date, "units": units,
                   "last_raw_price": fill["raw_open"], "last_adj_factor": fill["adj_factor"],
                   "last_quote_date": fill["execution_date"], "stale_days": 0, "non_tradable": False}
            self.lots.setdefault(code, []).append(lot)
        else:
            remaining = units
            candidates = sorted((lot for lot in self.lots.get(code, [])
                                 if lot["units"] > 0 and lot["sellable_date"] is not None
                                 and lot["sellable_date"] <= fill["execution_date"] and not lot["non_tradable"]),
                                key=lambda lot: (lot["sellable_date"], lot["buy_date"], lot["lot_id"]))
            for lot in candidates:
                taken = min(remaining, lot["units"])
                lot["units"] -= taken
                remaining -= taken
                if remaining <= 1e-12:
                    break
            if remaining > 1e-8:
                self.oversell_count += 1
                raise ValueError(f"Actual sellable units are insufficient for {code}")
            net_proceeds = notional - fees
            if net_proceeds >= 0:
                self.sale_restricted_cash += net_proceeds
            else:
                self.available_cash += net_proceeds
        if self.available_cash < -1e-7 or self.sale_restricted_cash < -1e-7 or self.other_frozen_cash < -1e-7:
            self.negative_cash_count += 1
            raise ValueError(f"R1 cash bucket became negative: {fill['execution_date']} {code} {side} "
                             f"{self.available_cash} {self.sale_restricted_cash} {self.other_frozen_cash}")

    def mark_and_value(self, day, facts):
        reference_value = 0.0
        stress_value = 0.0
        stale_value = 0.0
        non_tradable_value = 0.0
        lot_rows = []
        for code in sorted(self.lots):
            open_lots = [lot for lot in self.lots[code] if lot["units"] > 1e-12]
            if not open_lots:
                continue
            fact = facts.get(code, {})
            close, adj = fact.get("close"), fact.get("adj_factor")
            valid_quote = (fact.get("quotation_exists") is True and close is not None and adj is not None
                           and math.isfinite(float(close)) and math.isfinite(float(adj))
                           and float(close) > 0 and float(adj) > 0)
            non_tradable = fact.get("is_delisted") is True or fact.get("is_listed") is False
            for lot in open_lots:
                if valid_quote:
                    lot["last_raw_price"] = float(close)
                    lot["last_adj_factor"] = float(adj)
                    lot["last_quote_date"] = day
                    lot["stale_days"] = 0
                else:
                    lot["stale_days"] += 1
                lot["non_tradable"] = lot["non_tradable"] or non_tradable
                value = lot["units"] * lot["last_raw_price"] * lot["last_adj_factor"]
                if lot["non_tradable"] or lot["stale_days"] >= 120:
                    stress_factor = 0.0
                elif lot["stale_days"] >= 60:
                    stress_factor = 0.5
                else:
                    stress_factor = 1.0
                reference_value += value
                stress_value += value * stress_factor
                if lot["stale_days"] > 0:
                    stale_value += value
                if lot["non_tradable"]:
                    non_tradable_value += value
                sellable = (lot["units"] if lot["sellable_date"] is not None and lot["sellable_date"] <= day
                            and not lot["non_tradable"] else 0.0)
                lot_rows.append({"valuation_date": day, "ts_code": code, "lot_id": lot["lot_id"],
                    "buy_date": lot["buy_date"], "sellable_date": lot["sellable_date"],
                    "units": lot["units"], "sellable_units": sellable,
                    "frozen_units": lot["units"] - sellable, "non_tradable": lot["non_tradable"],
                    "last_raw_price": lot["last_raw_price"], "last_adj_factor": lot["last_adj_factor"],
                    "last_quote_date": lot["last_quote_date"], "stale_trade_days": lot["stale_days"],
                    "reference_value_cny": value, "stress_value_cny": value * stress_factor,
                    "accounting_mode": "adjusted_unit_proxy"})
        return {"reference_position_value_cny": reference_value,
                "stress_position_value_cny": stress_value,
                "stale_position_value_cny": stale_value,
                "non_tradable_position_value_cny": non_tradable_value,
                "position_lot": lot_rows}
