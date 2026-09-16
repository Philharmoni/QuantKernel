from datetime import date
import json

import pytest

from core.calendar import CalendarRangeError, TradingCalendar, build_calendar
from core.config import Paths
from core.data.store import Store


@pytest.fixture
def calendar(raw_rows):
    return TradingCalendar.from_rows(raw_rows("trade_cal"))


def test_manual_calendar_oracle(calendar, golden_root):
    expected = json.loads((golden_root / "expected.json").read_text(encoding="utf-8"))
    assert len(calendar.trade_dates) == expected["trading_days"]
    for case in expected["calendar_cases"]:
        if "is_trade_date" in case:
            assert calendar.is_trade_date(case["date"]) == case["is_trade_date"]
        if "error" in case:
            with pytest.raises(CalendarRangeError):
                if "offset" in case:
                    calendar.offset(case["date"], case["offset"])
                else:
                    calendar.next(case["date"])
        else:
            actual = calendar.next(case["date"]).strftime("%Y%m%d")
            assert actual == case["next_trade_date"]


def test_weekend_year_month_and_announcements(calendar):
    assert calendar.next("20181221") == date(2018, 12, 24)
    assert calendar.previous("2019-01-01") == date(2018, 12, 31)
    assert calendar.offset(date(2018, 12, 31), 1) == date(2019, 1, 2)
    assert calendar.offset("20190102", -1) == date(2018, 12, 31)
    assert calendar.offset("20190104", 1) == date(2019, 1, 7)
    assert calendar.offset("20190104", 0) == date(2019, 1, 4)
    assert calendar.available_date("20181222") == date(2018, 12, 24)
    assert calendar.available_date("20190104") == date(2019, 1, 7)
    assert calendar.available_date("20190131") == date(2019, 2, 1)
    assert not calendar.is_trade_date("2019-02-08")


def test_offset_requires_trading_day_and_integer(calendar):
    with pytest.raises(ValueError, match="requires a trading date"):
        calendar.offset("20190101", 1)
    with pytest.raises(ValueError, match="integer"):
        calendar.offset("20190102", 1.5)


@pytest.mark.parametrize("method,value", [
    ("previous", "20181217"), ("next", "20190201"),
    ("next", "20190204"), ("is_trade_date", "20181216"),
    ("available_date", "20190209"),
])
def test_calendar_bounds_are_explicit(calendar, method, value):
    with pytest.raises(CalendarRangeError, match="calendar coverage"):
        getattr(calendar, method)(value)


def test_selects_only_sse(raw_rows):
    rows = raw_rows("trade_cal")
    # A conflicting other-exchange row must not contaminate the SSE source.
    rows += [dict(exchange="CFFEX", cal_date="20190101", is_open="1")]
    assert not TradingCalendar.from_rows(rows).is_trade_date("20190101")
    with pytest.raises(ValueError, match="no source rows"):
        TradingCalendar.from_rows(rows, exchange="MISSING")


@pytest.mark.parametrize("opened", ["1", "0"])
def test_duplicate_and_conflicting_source_days_fail(raw_rows, opened):
    rows = raw_rows("trade_cal")
    rows.append(dict(rows[0], is_open=opened))
    with pytest.raises(ValueError, match="duplicate/conflicting cal_date"):
        TradingCalendar.from_rows(rows)


def test_missing_closed_calendar_day_fails(raw_rows):
    rows = [row for row in raw_rows("trade_cal") if row["cal_date"] != "20181222"]
    with pytest.raises(ValueError, match="missing natural calendar date 2018-12-22"):
        TradingCalendar.from_rows(rows)


@pytest.mark.parametrize("field,value,message", [
    ("cal_date", "20190230", "cal_date is invalid"),
    ("is_open", "2", "is_open must be 0 or 1"),
    ("is_open", None, "is_open must be 0 or 1"),
])
def test_invalid_source_dates_and_open_flags_fail(raw_rows, field, value, message):
    rows = raw_rows("trade_cal")
    rows[0][field] = value
    with pytest.raises(ValueError, match=message):
        TradingCalendar.from_rows(rows)


def test_no_open_days_fail(raw_rows):
    rows = [dict(row, is_open="0") for row in raw_rows("trade_cal")]
    with pytest.raises(ValueError, match="no open trading dates"):
        TradingCalendar.from_rows(rows)


def test_build_publishes_unique_dates_and_natural_bounds(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        manifest = build_calendar(store)
        assert manifest["rows"] == 34
        assert manifest["primary_key"] == ["trade_date"]
        restored = TradingCalendar.from_store(store)
        assert restored.calendar_start == date(2018, 12, 17)
        assert restored.calendar_end == date(2019, 2, 8)
        assert restored.trade_dates[-1] == date(2019, 2, 1)
        assert restored.available_date("20181222") == date(2018, 12, 24)
        rows = store.rows(f"SELECT * FROM {store.output_relation('l1', 'daily_calendar')}")
        assert len({row["trade_date"] for row in rows}) == len(rows)
        assert rows[0]["prev_trade_date"] is None
        assert rows[-1]["next_trade_date"] is None
        assert [row["trade_index"] for row in rows] == list(range(1, 35))


def test_invalid_source_does_not_publish(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "trade_cal.csv").write_text(
        "exchange,cal_date,is_open,pretrade_date\n"
        "SSE,20181217,1,20181214\nSSE,20181219,1,20181218\n", encoding="utf-8")
    with Store(Paths(raw, tmp_path / "middle")) as store:
        with pytest.raises(ValueError, match="missing natural calendar date"):
            build_calendar(store)
        assert not store.paths.output("l1", "daily_calendar", "part-00000.parquet").exists()
