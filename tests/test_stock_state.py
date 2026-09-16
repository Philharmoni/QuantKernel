"""Historical identity and missing-source boundaries, without market-data dependencies."""
import csv
from datetime import date
import shutil

import pytest

from core.calendar import build_calendar
from core.config import Paths
from core.data.store import Store
from core.market.state import build_stock_state


def _copy_identity_sources(golden_root, destination):
    destination.mkdir()
    for table in ("trade_cal", "stock_basic"):
        shutil.copy2(golden_root / f"{table}.csv", destination / f"{table}.csv")
    return destination


def _rewrite_basic(raw, change):
    path = raw / "stock_basic.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _build_state(raw, middle, **kwargs):
    with Store(Paths(raw, middle)) as store:
        build_calendar(store)
        build_stock_state(store, **kwargs)
        relation = store.output_relation("l1", "daily_stock_state")
        return store.rows(f"SELECT * FROM {relation} ORDER BY trade_date, ts_code")


def test_identity_grid_preserves_later_delisted_and_newly_listed_stocks(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "identity_only")
    rows = _build_state(raw, tmp_path / "middle")
    indexed = {(row["ts_code"], row["trade_date"]): row for row in rows}
    assert len(rows) == len(indexed) == 34 * 5
    assert not (raw / "stk_factor_pro.csv").exists()

    before_delist = indexed[("600001.SH", date(2019, 1, 22))]
    assert before_delist["is_listed"] is True
    assert before_delist["is_delisted"] is False
    assert before_delist["listing_trade_days"] == 26
    on_delist = indexed[("600001.SH", date(2019, 1, 23))]
    assert on_delist["is_listed"] is False
    assert on_delist["is_delisted"] is True
    assert on_delist["is_not_yet_listed"] is False
    assert on_delist["listing_trade_days"] == 26
    assert indexed[("600001.SH", date(2019, 2, 1))]["listing_trade_days"] == 26

    before_ipo = indexed[("000003.SZ", date(2019, 1, 8))]
    assert before_ipo["is_listed"] is False
    assert before_ipo["is_not_yet_listed"] is True
    assert before_ipo["is_delisted"] is False
    assert before_ipo["listing_trade_days"] == 0
    for day, age in [(9, 1), (10, 2)]:
        ipo = indexed[("000003.SZ", date(2019, 1, day))]
        assert ipo["is_listed"] is True
        assert ipo["is_not_yet_listed"] is False
        assert ipo["listing_trade_days"] == age
        assert ipo["listing_trade_days_lower_bound"] == age

    first = indexed[("000001.SZ", date(2018, 12, 17))]
    assert first["source_table"] == "stock_basic"
    assert first["source_file"] == "stock_basic.csv"
    assert first["source_row"] == 0
    assert first["exchange"] == "SZSE"


def test_missing_historical_status_is_typed_unknown_even_with_fixture_history(tmp_path, golden_root):
    assert (golden_root / "stock_state_history.csv").exists()
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        build_calendar(store)
        build_stock_state(store)
        relation = store.output_relation("l1", "daily_stock_state")
        rows = store.rows(f"SELECT * FROM {relation}")
        types = store.columns(relation)
        for field in ("is_st", "is_star_st", "is_delisting_period"):
            assert types[field] == "BOOLEAN"
            assert all(row[field] is None for row in rows)
        assert types["board"] == "VARCHAR"
        assert all(row["board"] is None for row in rows)
        for capability in ("historical_st", "historical_delisting_period"):
            status = store.rules["capabilities"][capability]
            assert status["status"] == "deferred"
            assert status["reason"]
        assert {"name", "list_status", "delist_date"}.isdisjoint(types)


def test_current_name_and_status_do_not_rewrite_historical_identity(tmp_path, golden_root):
    changed = _copy_identity_sources(golden_root, tmp_path / "changed_raw")

    def change_snapshot(rows):
        for row in rows:
            row["name"] = "*ST current snapshot only"
            row["list_status"] = "D" if row["list_status"] == "L" else "L"

    _rewrite_basic(changed, change_snapshot)
    before = _build_state(golden_root, tmp_path / "before")
    after = _build_state(changed, tmp_path / "after")
    assert before == after


def test_future_delisting_date_change_keeps_prior_rows_identical(tmp_path, golden_root):
    changed = _copy_identity_sources(golden_root, tmp_path / "changed_raw")

    def move_future_delisting(rows):
        next(row for row in rows if row["ts_code"] == "600001.SH")["delist_date"] = "20190125"

    _rewrite_basic(changed, move_future_delisting)
    before = _build_state(golden_root, tmp_path / "before")
    after = _build_state(changed, tmp_path / "after")
    cutoff = date(2019, 1, 23)
    assert [row for row in before if row["trade_date"] < cutoff] == [
        row for row in after if row["trade_date"] < cutoff
    ]
    changed_day = next(
        row for row in after
        if row["ts_code"] == "600001.SH" and row["trade_date"] == cutoff
    )
    assert changed_day["is_listed"] is True


def test_old_listing_uses_unknown_exact_age_and_observed_lower_bound(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "old_raw")

    def change_listing(rows):
        next(row for row in rows if row["ts_code"] == "000001.SZ")["list_date"] = "20170101"

    _rewrite_basic(raw, change_listing)
    rows = _build_state(raw, tmp_path / "middle")
    old = [row for row in rows if row["ts_code"] == "000001.SZ"]
    exact = next(row for row in rows if row["ts_code"] == "000002.SZ")
    assert len(old) == 34
    assert all(row["is_listed"] is True for row in old)
    assert all(row["listing_trade_days"] is None for row in old)
    assert [row["listing_trade_days_lower_bound"] for row in old] == list(range(1, 35))
    assert all(row["listing_age_status"] for row in old)
    assert all(row["listing_age_status"] != exact["listing_age_status"] for row in old)


def test_historical_t_prefixed_security_delisted_before_calendar_is_preserved(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "old_delisted_raw")

    def add_old_identity(rows):
        old = dict(next(row for row in rows if row["ts_code"] == "600001.SH"))
        old.update(
            ts_code="T600099.SH", name="Artificial historical retired code",
            list_date="20160101", delist_date="20171231"
        )
        rows.append(old)

    _rewrite_basic(raw, add_old_identity)
    rows = _build_state(raw, tmp_path / "middle")
    old = [row for row in rows if row["ts_code"] == "T600099.SH"]
    assert len(rows) == 34 * 6
    assert len(old) == 34
    assert all(row["is_delisted"] is True for row in old)
    assert all(row["is_listed"] is False for row in old)
    assert all(row["listing_trade_days"] is None for row in old)
    assert all(row["listing_trade_days_lower_bound"] == 0 for row in old)


def test_state_date_slice_matches_full_build_without_resetting_listing_age(tmp_path, golden_root):
    full = _build_state(golden_root, tmp_path / "full")
    sliced = _build_state(golden_root, tmp_path / "slice", start="20190110", end="20190124")
    assert sliced == [
        row for row in full if date(2019, 1, 10) <= row["trade_date"] <= date(2019, 1, 24)
    ]
    ipo = next(row for row in sliced if row["ts_code"] == "000003.SZ")
    assert ipo["trade_date"] == date(2019, 1, 10)
    assert ipo["listing_trade_days"] == 2


@pytest.mark.parametrize("field,value", [
    ("list_date", "20190230"),
    ("list_date", ""),
    ("delist_date", "20190230"),
    ("delist_date", "20181216"),
])
def test_invalid_listing_dates_fail_before_publication(tmp_path, golden_root, field, value):
    raw = _copy_identity_sources(golden_root, tmp_path / "raw")
    _rewrite_basic(raw, lambda rows: rows[0].__setitem__(field, value))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        with pytest.raises(ValueError, match="stock_basic|list_date|delist_date|listing"):
            build_stock_state(store)
        assert not store.paths.output("l1", "daily_stock_state", "part-00000.parquet").exists()


def test_duplicate_basic_code_fails_before_publication(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "raw")
    _rewrite_basic(raw, lambda rows: rows.append(dict(rows[0])))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        with pytest.raises(ValueError, match="duplicate"):
            build_stock_state(store)
        assert not store.paths.output("l1", "daily_stock_state", "part-00000.parquet").exists()
