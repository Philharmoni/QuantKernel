"""Independent price oracles and temporal invariance using artificial sources only."""
import csv
from datetime import date, datetime
import json
import shutil

import pytest

from core.calendar import build_calendar
from core.config import Paths
from core.data.store import Store
from core.market.prices import build_adjusted_price


def _read_output(store):
    relation = store.output_relation("l1", "daily_adjusted_price")
    return store.rows(f"SELECT * FROM {relation} ORDER BY trade_date, ts_code")


def _copy_sources(golden_root, destination):
    destination.mkdir()
    for table in ("trade_cal", "stk_factor_pro"):
        shutil.copy2(golden_root / f"{table}.csv", destination / f"{table}.csv")
    return destination


def _rewrite_prices(raw, change):
    path = raw / "stk_factor_pro.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _build_rows(raw, middle, **kwargs):
    with Store(Paths(raw, middle)) as store:
        build_calendar(store)
        build_adjusted_price(store, **kwargs)
        return _read_output(store)


def test_adjustment_and_returns_follow_manual_oracle(tmp_path, golden_root, raw_rows):
    expected = json.loads((golden_root / "expected.json").read_text(encoding="utf-8"))
    rows = _build_rows(golden_root, tmp_path / "middle")
    assert len(rows) == len(raw_rows("stk_factor_pro"))
    indexed = {(row["ts_code"], row["trade_date"]): row for row in rows}
    assert len(indexed) == len(rows)

    for case in expected["adjusted_price_cases"]:
        key = (case["ts_code"], datetime.strptime(case["trade_date"], "%Y%m%d").date())
        if case.get("quotation_exists") is False:
            assert key not in indexed
            continue
        actual = indexed[key]
        for field in ("close", "adj_factor", "adjusted_close"):
            assert float(actual[field]) == pytest.approx(case[field]), (key, field)
        if case["ret_1d"] is None:
            assert actual["ret_1d"] is None
            assert actual["return_status"] == "missing_previous_trading_day"
        else:
            assert actual["ret_1d"] == pytest.approx(case["ret_1d"])
            assert actual["return_status"] == "available"

    split = indexed[("000001.SZ", date(2019, 1, 8))]
    for field in ("open", "high", "low", "close"):
        assert float(split[field]) == 49.5
        assert split[f"adjusted_{field}"] == 99
    assert indexed[("000001.SZ", date(2019, 1, 4))]["prev_quote_date"] == date(2019, 1, 2)
    assert indexed[("000001.SZ", date(2019, 1, 10))]["prev_quote_date"] == date(2019, 1, 8)

    first = indexed[("000001.SZ", date(2018, 12, 17))]
    assert first["ret_1d"] is None
    assert first["prev_quote_date"] is None
    assert first["return_status"] == "no_previous_quote"
    assert {"vol", "amount", "source_file", "source_row", "source_table"} <= first.keys()
    assert first["source_file"] == "stk_factor_pro.csv"
    assert first["source_row"] == 0
    assert first["source_table"] == "stk_factor_pro"


def test_date_slice_retains_prior_quote_for_its_first_day(tmp_path, golden_root):
    full = _build_rows(golden_root, tmp_path / "full")
    sliced = _build_rows(
        golden_root, tmp_path / "slice", start="20190107", end="20190111"
    )
    expected = [row for row in full if date(2019, 1, 7) <= row["trade_date"] <= date(2019, 1, 11)]
    assert sliced == expected
    first = next(row for row in sliced if row["ts_code"] == "000001.SZ")
    assert first["trade_date"] == date(2019, 1, 7)
    assert first["prev_quote_date"] == date(2019, 1, 4)
    assert first["ret_1d"] == pytest.approx(-0.1)


def test_future_price_and_factor_changes_do_not_rewrite_the_past(tmp_path, golden_root):
    changed = _copy_sources(golden_root, tmp_path / "future_raw")

    def change_future(rows):
        for row in rows:
            if row["ts_code"] == "000001.SZ" and row["trade_date"] == "20190201":
                for field in ("open", "high", "low", "close"):
                    row[field] = str(float(row[field]) * 1.7)
                row["adj_factor"] = "9"

    _rewrite_prices(changed, change_future)
    before = _build_rows(golden_root, tmp_path / "before")
    after = _build_rows(changed, tmp_path / "after")
    assert [row for row in before if row["trade_date"] < date(2019, 2, 1)] == [
        row for row in after if row["trade_date"] < date(2019, 2, 1)
    ]
    future_key = ("000001.SZ", date(2019, 2, 1))
    before_future = next(row for row in before if (row["ts_code"], row["trade_date"]) == future_key)
    after_future = next(row for row in after if (row["ts_code"], row["trade_date"]) == future_key)
    assert before_future["adjusted_close"] != after_future["adjusted_close"]


@pytest.mark.parametrize("field,value", [
    ("adj_factor", "0"),
    ("adj_factor", "-1"),
    ("adj_factor", "NaN"),
    ("adj_factor", "inf"),
    ("close", "0"),
    ("open", "NaN"),
    ("high", "inf"),
    ("low", "-1"),
    ("trade_date", "20190230"),
    ("trade_date", "20190101"),
])
def test_invalid_price_inputs_fail_before_publication(tmp_path, golden_root, field, value):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _rewrite_prices(raw, lambda rows: rows[0].__setitem__(field, value))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        with pytest.raises(ValueError, match="stk_factor_pro|trade_date|OHLC|adj_factor|price"):
            build_adjusted_price(store)
        assert not store.paths.output("l1", "daily_adjusted_price", "part-00000.parquet").exists()


def test_duplicate_price_key_fails_before_publication(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _rewrite_prices(raw, lambda rows: rows.append(dict(rows[0])))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        with pytest.raises(ValueError, match="duplicate"):
            build_adjusted_price(store)
        assert not store.paths.output("l1", "daily_adjusted_price", "part-00000.parquet").exists()


def test_duplicate_prior_quote_cannot_silently_change_a_slice_return(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def add_conflicting_prior_quote(rows):
        previous = next(
            row for row in rows
            if row["ts_code"] == "000001.SZ" and row["trade_date"] == "20190104"
        )
        conflict = dict(previous)
        for field in ("open", "high", "low", "close"):
            conflict[field] = "120"
        rows.append(conflict)

    _rewrite_prices(raw, add_conflicting_prior_quote)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        with pytest.raises(ValueError, match="duplicate"):
            build_adjusted_price(store, start="20190107", end="20190111")
        assert not store.paths.output("l1", "daily_adjusted_price", "part-00000.parquet").exists()
