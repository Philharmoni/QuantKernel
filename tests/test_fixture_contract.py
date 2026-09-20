"""Step 2 checks the manual oracle and scenario coverage, not production rules."""
from datetime import datetime
import json


def test_golden_size_dates_and_price_key(raw_rows, golden_root):
    stocks = raw_rows("stock_basic")
    calendar = raw_rows("trade_cal")
    dates = [row["cal_date"] for row in calendar if row["is_open"] == "1"]
    assert 3 <= len(stocks) <= 5
    assert 20 <= len(dates) <= 40
    assert len(dates) == len(set(dates))
    for date in dates:
        datetime.strptime(date, "%Y%m%d")
    prices = raw_rows("stk_factor_pro")
    keys = [(row["trade_date"], row["ts_code"]) for row in prices]
    assert len(keys) == len(set(keys))
    assert set(row["trade_date"] for row in prices) <= set(dates)
    assert json.loads((golden_root / "expected.json").read_text(encoding="utf-8"))


def test_delisting_and_ipo_are_historical_scenarios(raw_rows):
    stocks = {row["ts_code"]: row for row in raw_rows("stock_basic")}
    assert stocks["600001.SH"]["delist_date"] == "20190123"
    assert stocks["000003.SZ"]["list_date"] == "20190109"
    assert any(row["ts_code"] == "600001.SH" and row["trade_date"] < "20190123"
               for row in raw_rows("stk_factor_pro"))


def test_st_intervals_are_complete_nonoverlapping_and_historical(raw_rows):
    history = raw_rows("namechange")
    stocks = {row["ts_code"] for row in raw_rows("stock_basic")}
    for code in stocks:
        intervals = sorted((row["start_date"], row["end_date"] or "99999999")
                           for row in history if row["ts_code"] == code)
        assert intervals, code
        for (start, end), (next_start, _) in zip(intervals, intervals[1:]):
            assert next_start > end, (code, intervals)
        if code == "000002.SZ":
            names = {row["name"] for row in history if row["ts_code"] == code}
            assert any("ST" in name.upper() for name in names)
            assert any("*ST" in name.upper() for name in names)


def test_namechange_spans_cover_the_full_listing_period(raw_rows):
    history = raw_rows("namechange")
    for stock in raw_rows("stock_basic"):
        spans = sorted((row["start_date"], row["end_date"] or "99999999")
                       for row in history if row["ts_code"] == stock["ts_code"])
        assert spans, stock["ts_code"]
        # The first recorded name starts with the listing itself.
        assert spans[0][0] == stock["list_date"], (stock["ts_code"], spans)


def test_limit_prices_have_unique_keys_and_valid_placeholders(raw_rows):
    rows = raw_rows("stk_limit")
    keys = [(row["trade_date"], row["ts_code"]) for row in rows]
    assert len(keys) == len(set(keys))
    dates = [row["cal_date"] for row in raw_rows("trade_cal") if row["is_open"] == "1"]
    assert set(row["trade_date"] for row in rows) <= set(dates)
    ordinary = [row for row in rows if row["ts_code"] == "000001.SZ"]
    assert set(row["trade_date"] for row in ordinary) == {
        day for day in dates if day >= "20190104"}
    placeholder = next(row for row in rows if row["ts_code"] == "830001.BJ")
    assert float(placeholder["up_limit"]) == 99999.99
    assert float(placeholder["down_limit"]) == 0.0


def test_directional_limits_suspension_and_adjustment_are_present(raw_rows):
    suspended = raw_rows("suspend_d")
    assert any(row["ts_code"] == "000001.SZ" and row["trade_date"] == "20190103"
               and row["suspend_type"] == "S" for row in suspended)
    events = {(row["trade_date"], row["ts_code"]): row["limit"] for row in raw_rows("limit_list_d")}
    assert events[("20190104", "000001.SZ")] == "U"
    assert events[("20190107", "000001.SZ")] == "D"
    prices = {row["trade_date"]: row for row in raw_rows("stk_factor_pro") if row["ts_code"] == "000001.SZ"}
    assert "20190109" not in prices
    before, after = prices["20190107"], prices["20190108"]
    assert float(before["adj_factor"]) != float(after["adj_factor"])
    assert float(before["close"]) * float(before["adj_factor"]) == float(after["close"]) * float(after["adj_factor"])


def test_financial_oracle_has_weekend_revision_and_missing_quarter(raw_rows):
    rows = raw_rows("income_vip")
    dates = [row["cal_date"] for row in raw_rows("trade_cal") if row["is_open"] == "1"]
    def next_day(date):
        return next(day for day in dates if day > date)
    assert next_day("20181222") == "20181224"
    assert next_day("20190104") == "20190107"
    ordinary = [row for row in rows if row["ts_code"] == "000001.SZ"]
    assert any(row["ann_date"] == "20181222" for row in ordinary)
    assert len([row for row in ordinary if row["end_date"] == "20180630"]) == 2
    assert not any(row["ts_code"] == "000002.SZ" and row["end_date"] == "20180630" for row in rows)
