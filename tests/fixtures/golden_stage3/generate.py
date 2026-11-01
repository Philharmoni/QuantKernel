"""Generate wholly fictional L1/L2/L3-shaped inputs and hand-derived expectations.
No project data/, real parquet, network, or Stage 3 strategy implementation is used.
"""
import csv
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CODES = [f"SA{i:03d}.SZ" for i in range(1, 13)]
START, END = date(2020, 3, 2), date(2020, 6, 30)
HOLIDAYS = {date(2020, 4, 6)} | {date(2020, 5, day) for day in range(1, 6)}
DATES = []
current = START
while current <= END:
    if current.weekday() < 5 and current not in HOLIDAYS:
        DATES.append(current)
    current += timedelta(days=1)
assert len(DATES) == 83
T, D = 20, 21


def day(index):
    return DATES[index].isoformat()


def write(name, rows, fields=None):
    path = ROOT / name
    fields = fields or list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def main():
    calendar = [{"trade_date": day(i), "trade_index": i, "is_open": True,
                 "pretrade_date": day(i - 1) if i else ""} for i in range(len(DATES))]
    write("daily_calendar.csv", calendar)
    prices, statuses, states, universes, industries = [], [], [], [], []
    for i, current_date in enumerate(DATES):
        for j, code in enumerate(CODES):
            close = 10 + j
            adj = 1
            suspended = (code == "SA003.SZ" and i == D) or (code == "SA005.SZ" and 22 <= i <= 25)
            intraday = code == "SA003.SZ" and i == 22
            delisted = code == "SA006.SZ" and i >= 40
            st = code == "SA008.SZ" and i >= 30
            if code == "SA005.SZ" and i >= D:
                adj = 2
            if code == "SA006.SZ" and i >= 39:
                close = 20
            quoted = not suspended and not delisted
            up, down = close * 1.1, close * 0.9
            if code == "SA004.SZ" and i == D:
                close, up, down = 13.2, 13.2, 10.8
            if code == "SA002.SZ" and i == D:
                close, down = 9.9, 9.9
            if code == "SA010.SZ" and i == D:
                close = 20
            if code == "SA011.SZ" and i == D:
                up = None
            amount = 20000 if quoted else None
            prices.append({"trade_date": day(i), "ts_code": code, "open": close if quoted else "",
                "high": close if quoted else "", "low": close if quoted else "",
                "close": close if quoted else "", "pre_close": "",
                "adj_factor": adj, "adjusted_open": close * adj if quoted else "",
                "adjusted_high": close * adj if quoted else "", "adjusted_low": close * adj if quoted else "",
                "adjusted_close": close * adj if quoted else "", "vol": 1000 if quoted else "",
                "amount": amount, "total_mv": close * 10000, "circ_mv": close * 8000,
                "turnover_rate": 2, "ret_1d": "", "return_status": "fixture_input",
                "prev_quote_date": day(i - 1) if i and quoted else "", "source_file": "fictional", "source_row": i})
            full, missing_limit = suspended, code == "SA011.SZ" and i == D
            statuses.append({"trade_date": day(i), "ts_code": code, "quotation_exists": quoted,
                "is_full_day_suspended": full, "is_intraday_suspended": intraday,
                "is_suspended": full or intraday, "is_delisted": delisted,
                "is_listed": not delisted, "is_not_yet_listed": False,
                "up_limit": up if up is not None else "", "down_limit": down if down is not None else "",
                "limit_price_exists": not missing_limit, "price_limit_up": code == "SA004.SZ" and i == D,
                "price_limit_down": code == "SA002.SZ" and i == D, "is_limit_up": None,
                "is_limit_down": None, "can_buy": None, "can_sell": None,
                "cannot_buy_reason": "DAILY_STATE_NOT_PREOPEN_PERMISSION",
                "cannot_sell_reason": "DAILY_STATE_NOT_PREOPEN_PERMISSION",
                "observed_full_day": full, "observed_intraday": intraday, "observed_suspend": full or intraday,
                "observed_suspend_types": ["S"] if full else ["P"] if intraday else [],
                "has_resume_event": code == "SA005.SZ" and i == 26, "observed_limit_events": [],
                "observed_up": False, "observed_down": False, "observed_opened": False,
                "raw_close": close if quoted else "", "vol": 1000 if quoted else "", "amount": amount})
            exact = None if code == "SA010.SZ" else 200 + i
            lower = 200 + i if code == "SA010.SZ" else exact
            if code == "SA012.SZ":
                exact, lower = None, 99 + i
            states.append({"trade_date": day(i), "ts_code": code, "is_a_share": True,
                "is_listed": not delisted, "is_delisted": delisted, "is_not_yet_listed": False,
                "is_st": st, "is_star_st": st, "is_delisting_period": None, "board": None,
                "historical_name": ("ST " if st else "") + code, "historical_state_status": "available",
                "listing_trade_days": exact if exact is not None else "",
                "listing_trade_days_lower_bound": lower,
                "listing_natural_days": 500 + i, "estimated_listing_trade_days": 345,
                "listing_age_status": "calendar_left_truncated" if code == "SA010.SZ" else "exact",
                "exchange": "SZSE"})
            eligible = not delisted and not st and (lower >= 120) and code != "SA009.SZ"
            universes.append({"trade_date": day(i), "ts_code": code,
                "universe_id": "all_a_ex_st_ipo120_ex_bj", "config_id": "fixture_v1",
                "is_in_universe": eligible, "exclusion_reason": "" if eligible else "fixture_exclusion",
                "is_a_share": True, "is_listed": not delisted, "is_delisted": delisted,
                "is_st": st, "listing_trade_days": exact, "listing_trade_days_lower_bound": lower})
            in_date = day(30) if code == "SA007.SZ" else "2019-01-01"
            known = i >= 30 or code != "SA007.SZ"
            industries.append({"trade_date": day(i), "ts_code": code, "l1_code": "FIX_A",
                "l1_name": "FictionalA", "l2_code": "FIX_A", "l2_name": "FictionalA",
                "l3_code": "FIX_A", "l3_name": "FictionalA", "in_date": in_date, "out_date": "",
                "is_new": "Y", "industry_pit_status": "snapshot_after_in_date" if known else "snapshot_backfilled",
                "source_table": "index_member_all"})
    write("daily_adjusted_price.csv", prices)
    write("daily_trade_status.csv", statuses)
    write("daily_stock_state.csv", states)
    write("daily_research_universe.csv", universes)
    write("daily_industry.csv", industries)

    processed = []
    for i, code in enumerate(CODES):
        for factor in ("bp", "sp_ttm", "rev_20", "vol_20"):
            value = 1 if factor in ("bp", "sp_ttm") else -0.6 if factor == "rev_20" else -0.3
            if code == "SA004.SZ":
                value = {"bp": 2, "sp_ttm": 0, "rev_20": -1.2, "vol_20": -0.6}[factor]
            if code == "SA005.SZ":
                value = 0
            if code == "SA003.SZ" and factor == "sp_ttm":
                value = ""
            if i in (9, 10, 11) and code in CODES[:2]:
                value = ""
            for process in ("ws_z", "ws_z_industry_mv_neutral_z"):
                processed.append({"trade_date": day(T), "ts_code": code, "factor_id": factor,
                    "value": value, "universe_id": "all_a_ex_st_ipo120_ex_bj", "process_id": process})
    for code in CODES:
        for factor in ("bp", "sp_ttm", "rev_20", "vol_20"):
            processed.append({"trade_date": day(50), "ts_code": code, "factor_id": factor, "value": 1,
                "universe_id": "all_a_ex_st_ipo120_ex_bj", "process_id": "ws_z"})
    write("factor_processed.csv", processed)

    write("financial_available.csv", [
        {"source_table": "income_vip", "record_id": "original", "ts_code": "SA001.SZ", "end_date": "2019-12-31",
         "effective_ann_date": day(T - 1), "available_date": day(T), "availability_status": "available", "value": 100},
        {"source_table": "income_vip", "record_id": "revision", "ts_code": "SA001.SZ", "end_date": "2019-12-31",
         "effective_ann_date": day(T), "available_date": day(D), "availability_status": "available", "value": 120}])
    members = []
    for i in range(len(DATES)):
        for offset, code in enumerate(("SA001.SZ", "SA002.SZ", "SA004.SZ")):
            members.append({"trade_date": day(i), "index_code": "FIXINDEX", "con_code": code,
                "weight_trade_date": day(T - 5), "available_date": day(T), "weight": (offset + 1) * 10})
        if i >= T + 1:
            members.append({"trade_date": day(i), "index_code": "FIXINDEX", "con_code": "SA003.SZ",
                "weight_trade_date": day(T - 4), "available_date": day(T + 1), "weight": 40})
    write("daily_index_member.csv", members)
    labels = []
    for horizon in (1, 5, 20):
        labels.append({"trade_date": day(T), "ts_code": "SA001.SZ", "label_id": f"ret_o2o_{horizon}",
            "horizon": horizon, "entry_date": day(D), "planned_exit_date": day(D + horizon),
            "entry_price": 10, "exit_date": day(D + horizon), "exit_price": 10, "ret": 0,
            "exception_flag": "", "entry_blocked": False, "entry_open_at_up_limit": False,
            "exit_delayed_days": 0, "delisted_in_window": False, "beyond_data_end": False})
    labels.append({"trade_date": "2024-12-31", "ts_code": "SA001.SZ", "label_id": "ret_o2o_5", "horizon": 5,
        "entry_date": "2025-01-02", "planned_exit_date": "2025-01-09", "entry_price": 10,
        "exit_date": "2025-01-09", "exit_price": 11, "ret": 0.1, "exception_flag": "permission_test_only",
        "entry_blocked": False, "entry_open_at_up_limit": False, "exit_delayed_days": 0,
        "delisted_in_window": False, "beyond_data_end": False})
    write("forward_return_research_only.csv", labels)

    expected = {
        "fixture_version": 1, "calendar": {"trading_days": 83, "T": day(T), "D": day(D),
            "holidays": sorted(x.isoformat() for x in HOLIDAYS if x.weekday() < 5),
            "calendar_declared_non_open_dates": sorted(x.isoformat() for x in HOLIDAYS)},
        "signal": {"SA001.SZ": {"value_module": 1, "reversal_module": 0.6, "low_vol_module": 0.3,
            "score": 0.6333333333333333, "formula": "((1+1)/2 + 0.6 + 0.3)/3"},
            "SA003.SZ": "excluded_missing_sp_ttm", "all_equal_at": day(50)},
        "fixture_overrides": {"target_count": 3, "retain_rank_through": 4,
            "universe_min_adv20_cny": 0, "min_cross_section": 4, "min_common_factor_coverage": 0.5,
            "min_listing_trade_days": 120},
        "buffer": {"ranked": ["SA001.SZ", "SA002.SZ", "SA004.SZ", "SA003.SZ", "SA005.SZ"],
            "held": ["SA003.SZ", "SA004.SZ"], "target": ["SA001.SZ", "SA004.SZ", "SA003.SZ"],
            "rule": "retain held ranks <=4, fill to 3 by score; SA002 displaced"},
        "pit": {"original_visible": day(T), "revision_not_visible_until": day(D),
            "SA007_before_in_date": "unknown", "SA007_from": day(30),
            "index_member_first_visible": day(T), "new_member_first_visible": day(D),
            "T_decision_must_ignore_D_close_and_amount": True},
        "execution": {"SA004.SZ": {"date": day(D), "side": "BUY", "open": 13.2, "up_limit": 13.2, "result": "reject_open_up_limit"},
            "SA002.SZ": {"date": day(D), "side": "SELL", "open": 9.9, "down_limit": 9.9, "result": "reject_open_down_limit"},
            "SA003.SZ": {"date": day(D), "result": "reject_full_day_suspension"},
            "SA003.SZ_intraday": {"date": day(22), "result": "reject_intraday_suspension_conservative"},
            "SA011.SZ": {"date": day(D), "result": "reject_missing_limit"},
            "SA010.SZ": {"date": day(D), "open": 20, "result": "same_T_quantity_intent_not_reoptimized"},
            "SA009.SZ": {"date": day(D), "adv20_t": 20000000, "full_day_amount_d": 16000000,
                "requested_notional": 200000, "fill_notional": 160000, "remaining_notional": 40000,
                "result": "partial_fill_no_replacement", "formula": "min(1%*20000000,1%*16000000)=160000"},
            "daily_amount_cap": "min(1% ADV20_T, 1% full_day_amount_D); D is upper bound only",
            "zero_fill_day": "cash_unchanged_no_nav_drop_from_unfilled_order"},
        "cash_fees": {"initial_cash": 10000, "reserved_fee": 10, "available_for_purchase": 9990,
            "buy_notional": 9990, "sell_proceeds": 5000, "sell_tax": 5,
            "same_day_buying_power_from_sale": 0, "next_day_available_sale_cash": 4995,
            "commission_small_order": 5, "commission_formula": "max(5, notional*0.0003)",
            "transfer_fee_formula": "notional*0.000002"},
        "accounting": {"mode": "adjusted_unit_proxy", "initial_cash": 1000,
            "buy": {"date": day(T), "ts_code": "SA005.SZ", "raw_price": 14, "adj_factor": 1, "units": 50, "cash_after": 300},
            "valuation_after_split": {"date": day(D), "raw_price": 14, "adj_factor": 2, "unit_value": 28,
                "position_value": 1400, "nav": 1700, "cash_dividend_added": 0},
            "real_share_account": "blocked_company_action_ledger"},
        "lifecycle": {"ST_exit_signal": day(30), "delisting_effective": day(40),
            "delisted_position": "retain_non_tradable_asset_no_last_price_fill",
            "stale_write_down": {60: 0.5, 120: 1.0}, "ordinary_order": "expire_after_D",
            "risk_exit": "resubmit_next_day", "T_plus_one_sell": True,
            "last_authorized_signal": "execution_beyond_permission_no_2025_fill"},
        "constraints": {"stock_budget": 0.95, "frozen_weight": 0.20, "free_weight": 0.75,
            "candidate_count": 3, "ideal_each_before_single_cap": 0.25,
            "after_single_cap": [0.02, 0.02, 0.02], "cash_after_infeasible_freeze": 0.86,
            "stock_after_infeasible_freeze": 0.14, "expected": "keep_positions_no_new_risk_risk_exit_only",
            "infeasible": "keep_positions_no_new_risk_risk_exit_only",
            "no_silent_constraint_relaxation": True, "frozen_weight_not_removed": True},
        "nav_reconciliation": {"initial_cash": 1000, "buy_notional": 600, "adj_factor": 1,
            "units": 60, "raw_price": 10, "commission": 5, "transfer_fee": 0.0012,
            "cash_after": 394.9988, "adj_factor_after": 2, "position_value": 1200,
            "nav_after": 1594.9988, "fees_total": 5.0012,
            "wealth_identity": "1594.9988=1200+394.9988; no second dividend or share credit"},
        "non_overlapping_periods": {"dates": ["D0", "D1", "D2", "D3", "D4"],
            "period_1_return": 0.10, "period_2_return": -0.05, "compound_return": 0.045,
            "formula": "(1+0.10)*(1-0.05)-1=0.045; each holding counted once"},
        "research_controls": {"eligible_budget": 2, "diagnostic_budget": 5,
            "two_concurrent_reservations": "exactly_one_winner", "oos_token": "reserved_before_read",
            "used_token_release": "denied", "corrupt_manifest": "reject", "duplicate_key": "reject",
            "interrupted_publish": "not_complete"},
        "permission_failures": ["entry_date_2025", "exit_date_2025", "iso_2025_date", "omitted_end",
            "full_calendar_scan_not_scope_proof", "physical_snapshot_or_bounded_read_certificate_required"],
        "research_failures": ["concurrent_trial_reservation_single_winner", "oos_token_reserved_before_read",
            "used_or_result_seen_token_not_released", "budget_2_candidates_5_diagnostics",
            "corrupt_input_manifest_reject", "duplicate_primary_key_reject", "interrupted_publish_not_complete"],
        "implementation_scope": "Expectations only. Steps 3-6 must implement and prove these cases; no strategy algorithm is provided by this fixture."
    }
    (ROOT / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "scenario_registry.json").write_text(json.dumps({
        "step1_only": True, "production_automatic_threshold_downgrade": False,
        "scenarios": [{"scenario_id": key, "expected_json_key": key} for key in expected if key not in ("fixture_version", "calendar")],
        "required_future_checks": ["future_quote_perturbation", "future_label_perturbation", "future_financial_perturbation",
            "future_industry_perturbation", "future_member_perturbation", "all_fixture_day_truncation",
            "two_independent_business_content_runs"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"fixture_version": 1, "fictional": True, "real_data_accessed": False,
        "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(ROOT.glob("*.csv")) + [ROOT / "expected.json", ROOT / "scenario_registry.json"]}}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Generated {len(DATES)} fictional trading days and {len(expected)} expectation sections")


if __name__ == "__main__":
    main()
