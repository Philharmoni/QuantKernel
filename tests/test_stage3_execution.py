import copy
from datetime import date, timedelta

import pytest

from core.strategy.accounting import R1Account
from core.strategy.contract import load_stage3_contract
from core.strategy.execution import CommissionLedger, constrained_notional, execution_eligibility
from core.strategy.r1_engine import R1Engine


D1, D2 = date(2020, 3, 31), date(2020, 4, 1)


def config():
    return copy.deepcopy(load_stage3_contract()["config"])


def fact(**changes):
    result = {"open": 10.0, "close": 10.0, "adj_factor": 1.0, "amount": 16_000.0,
              "is_st": False, "is_delisted": False, "is_listed": True,
              "quotation_exists": True, "limit_price_exists": True, "up_limit": 11.0, "down_limit": 9.0,
              "is_suspended": False, "is_full_day_suspended": False, "is_intraday_suspended": False}
    result.update(changes)
    return result


def order(code, day, side="BUY", notional=200_000.0, purpose="rebalance"):
    return {"order_id": f"{code}-{day}-{side}-{purpose}", "signal_date": day - timedelta(days=1),
            "planned_execution_date": day, "ts_code": code, "side": side, "purpose": purpose,
            "reference_notional_cny": notional, "adv20_cny": 20_000_000.0,
            "return_volatility_20": 0.02, "order_status": "intent", "time_in_force": "DAY"}


def test_execution_eligibility_matrix_and_reverse_limit_rules():
    assert execution_eligibility(fact(), "BUY") == (True, "")
    assert execution_eligibility(fact(open=11.0), "BUY") == (False, "buy_open_at_up_limit")
    assert execution_eligibility(fact(open=11.0), "SELL") == (True, "")
    assert execution_eligibility(fact(open=9.0), "SELL") == (False, "sell_open_at_down_limit")
    assert execution_eligibility(fact(open=9.0), "BUY") == (True, "")
    assert execution_eligibility(fact(is_full_day_suspended=True), "BUY")[1] == "full_day_suspension"
    assert execution_eligibility(fact(is_intraday_suspended=True), "BUY")[1] == "intraday_suspension_conservative"
    assert execution_eligibility(fact(limit_price_exists=False), "BUY")[1] == "missing_limit"
    assert execution_eligibility(fact(open=12.0), "BUY")[1] == "open_outside_valid_limits"


def test_partial_fill_uses_both_amount_caps_and_expires_without_replacement():
    cfg = config()
    cfg["objective"]["initial_capital_cny"] = 1_000_000
    engine = R1Engine(cfg, [D1, D2], lambda day, codes: {code: fact() for code in codes},
                      [order("SA009.SZ", D1)])
    tables = engine.run()
    fill = tables["fill"][0]
    assert fill["notional_cny"] == pytest.approx(160_000)
    assert fill["fill_model"] == "proxy_not_observed"
    assert fill["liquidity_basis"] == "full_day_upper_bound"
    events = tables["order_event"]
    assert [event["event_type"] for event in events] == ["submitted", "partially_filled", "expired"]
    assert events[-1]["remaining_notional_cny"] == pytest.approx(40_000)
    assert tables["cash_ledger"][-1]["available_balance"] == pytest.approx(
        1_000_000 - 160_000 - fill["total_cost_cny"])


def test_raw_share_quantity_rules_cover_default_star_and_full_odd_lot_sales():
    cfg = config()
    assert constrained_notional(1_990, 10, 1, "600000.SH", "BUY", cfg) == (1_000, False)
    assert constrained_notional(990, 10, 1, "600000.SH", "BUY", cfg) == (0, False)
    assert constrained_notional(1_990, 10, 1, "688001.SH", "BUY", cfg) == (0, False)
    assert constrained_notional(2_510, 10, 1, "688001.SH", "BUY", cfg) == (2_510, False)
    assert constrained_notional(1_500, 10, 1, "600000.SH", "SELL", cfg, 250) == (1_000, False)
    assert constrained_notional(2_500, 10, 1, "600000.SH", "SELL", cfg, 250) == (2_500, True)
    assert constrained_notional(1_500, 10, 1, "688001.SH", "SELL", cfg, 150) == (1_500, True)


def test_actual_lots_enforce_t_plus_one_and_sale_cash_is_restricted():
    cfg = config()
    cfg["objective"]["initial_capital_cny"] = 1_000_000
    orders = [order("SA005.SZ", D1, notional=100_000),
              order("SA005.SZ", D1, side="SELL", notional=50_000),
              order("SA005.SZ", D2, side="SELL", notional=50_000)]
    tables = R1Engine(cfg, [D1, D2, D2 + timedelta(days=1)],
                      lambda day, codes: {code: fact() for code in codes}, orders).run()
    rejected = [event for event in tables["order_event"] if event["event_date"] == D1
                and event["side"] == "SELL" and event["event_type"] == "rejected"]
    assert rejected[0]["reason"] == "t_plus_one_or_no_sellable_position"
    assert len(tables["fill"]) == 2
    nav = {row["trade_date"]: row for row in tables["nav_daily"]}
    assert nav[D2]["sale_restricted_cash"] > 0
    assert nav[D2 + timedelta(days=1)]["sale_restricted_cash"] == 0
    assert nav[D2 + timedelta(days=1)]["available_cash"] > nav[D2]["available_cash"]
    day1_lot = tables["position_lot"][0]
    assert day1_lot["sellable_units"] == 0
    assert day1_lot["frozen_units"] == pytest.approx(day1_lot["units"])


def test_adjusted_unit_split_has_no_extra_dividend_and_sale_cash_releases_next_day():
    cfg = config()
    cfg["objective"]["initial_capital_cny"] = 1_000
    account = R1Account(cfg)
    fill = {"fill_id": "golden-buy", "execution_date": D1, "ts_code": "SA005.SZ", "side": "BUY",
            "raw_open": 14.0, "adj_factor": 1.0, "notional_cny": 700.0, "units": 50.0, "total_cost_cny": 0.0}
    account.apply_fill(fill, D2)
    valuation = account.mark_and_value(D2, {"SA005.SZ": fact(close=14.0, adj_factor=2.0)})
    assert account.available_cash == pytest.approx(300)
    assert valuation["reference_position_value_cny"] == pytest.approx(1400)
    assert account.total_cash + valuation["reference_position_value_cny"] == pytest.approx(1700)
    sale = dict(fill, fill_id="golden-sale", execution_date=D2, side="SELL", raw_open=14.0,
                adj_factor=2.0, notional_cny=1400.0, units=50.0, total_cost_cny=5.0)
    account.apply_fill(sale, D2 + timedelta(days=1))
    assert account.available_cash == pytest.approx(300)
    assert account.sale_restricted_cash == pytest.approx(1395)
    assert account.release_sale_cash() == pytest.approx(1395)
    assert account.available_cash == pytest.approx(1695)


def test_minimum_commission_is_aggregated_per_stock_side_and_day():
    ledger = CommissionLedger(config())
    key = (D1, "SA001.SZ", "BUY")
    assert ledger.charge(key, 1_000) == pytest.approx(5)
    assert ledger.charge(key, 1_000) == pytest.approx(0)
    assert ledger.paid[key] == pytest.approx(5)


def test_risk_exit_is_resubmitted_next_day_after_suspension():
    cfg = config()
    cfg["objective"]["initial_capital_cny"] = 1_000_000
    d0 = D1 - timedelta(days=1)
    orders = [order("SA003.SZ", d0, notional=100_000),
              order("SA003.SZ", D1, side="SELL", notional=100_000, purpose="risk_exit")]
    def facts(day, codes):
        suspended = day == D1
        return {code: fact(is_suspended=suspended, is_full_day_suspended=suspended) for code in codes}
    tables = R1Engine(cfg, [d0, D1, D2], facts, orders).run()
    assert any(event["event_date"] == D1 and event["event_type"] == "rejected"
               for event in tables["order_event"])
    assert any(fill["execution_date"] == D2 and fill["purpose"] == "risk_exit"
               for fill in tables["fill"])


def test_truncated_account_is_exact_prefix_of_full_run():
    cfg = config()
    cfg["objective"]["initial_capital_cny"] = 1_000_000
    orders = [order("SA005.SZ", D1, notional=100_000),
              order("SA005.SZ", D2, side="SELL", notional=50_000)]
    def build():
        engine = R1Engine(cfg, [D1, D2], lambda day, codes: {code: fact() for code in codes}, orders)
        return engine
    full = build().run()
    truncated = build().run(end_date=D1)
    date_fields = {"execution_eligibility": "execution_date", "order_event": "event_date",
                   "fill": "execution_date", "position_lot": "valuation_date"}
    for table, rows in full.items():
        field = date_fields.get(table, "trade_date")
        assert truncated[table] == [row for row in rows if row[field] <= D1]
