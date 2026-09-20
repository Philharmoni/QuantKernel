"""Historical identity, namechange-derived ST status, and missing-source boundaries."""
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
    for table in ("trade_cal", "stock_basic", "namechange"):
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


def _rewrite_namechange(raw, change):
    path = raw / "namechange.csv"
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


def test_st_status_is_derived_from_namechange_intervals(tmp_path, golden_root):
    rows = _build_state(golden_root, tmp_path / "middle")
    indexed = {(row["ts_code"], row["trade_date"]): row for row in rows}
    for day, st, star, name in [
        (date(2019, 1, 4), False, False, "Ordinary sample two"),
        (date(2019, 1, 7), True, False, "ST sample"),
        (date(2019, 1, 10), True, True, "*ST sample"),
        (date(2019, 1, 14), True, False, "ST sample"),
        (date(2019, 1, 15), True, False, "ST sample"),
        (date(2019, 1, 16), False, False, "Ordinary sample two"),
    ]:
        row = indexed[("000002.SZ", day)]
        assert row["is_st"] is st, (day, row)
        assert row["is_star_st"] is star, (day, row)
        assert row["historical_name"] == name, (day, row)
        assert row["historical_state_status"] == "namechange_derived"
        assert row["name_source_file"] == "namechange.csv"
    not_listed = indexed[("000003.SZ", date(2019, 1, 8))]
    assert not_listed["is_st"] is None
    assert not_listed["historical_name"] is None
    assert not_listed["historical_state_status"] == "not_listed"
    delisted = indexed[("600001.SH", date(2019, 1, 23))]
    assert delisted["is_st"] is None
    assert delisted["historical_state_status"] == "not_listed"


def test_delisting_period_and_board_stay_unknown_without_source(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        build_calendar(store)
        build_stock_state(store)
        relation = store.output_relation("l1", "daily_stock_state")
        rows = store.rows(f"SELECT * FROM {relation}")
        types = store.columns(relation)
        assert types["is_delisting_period"] == "BOOLEAN"
        assert all(row["is_delisting_period"] is None for row in rows)
        assert types["board"] == "VARCHAR"
        assert all(row["board"] is None for row in rows)
        capability = store.rules["capabilities"]["historical_delisting_period"]
        assert capability["status"] == "deferred"
        assert capability["reason"]
        assert store.rules["capabilities"]["historical_st"]["status"] == "active"


def test_uncovered_and_conflicting_namechange_intervals_stay_unknown(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "raw")

    def break_history(rows):
        # 000001.SZ loses its only interval; 000002.SZ gets a conflicting overlap.
        rows[:] = [row for row in rows if row["ts_code"] != "000001.SZ"]
        rows.append(dict(ts_code="000002.SZ", name="ST conflicting overlap", start_date="20190117",
                         end_date="", ann_date="20190117", change_reason="其他"))

    _rewrite_namechange(raw, break_history)
    rows = _build_state(raw, tmp_path / "middle")
    indexed = {(row["ts_code"], row["trade_date"]): row for row in rows}
    uncovered = indexed[("000001.SZ", date(2019, 1, 10))]
    assert uncovered["is_st"] is None
    assert uncovered["is_star_st"] is None
    assert uncovered["historical_name"] is None
    assert uncovered["historical_state_status"] == "namechange_uncovered"
    ambiguous = indexed[("000002.SZ", date(2019, 1, 17))]
    assert ambiguous["is_st"] is None
    assert ambiguous["historical_state_status"] == "namechange_ambiguous"


def test_agreeing_overlapping_intervals_derive_the_shared_value(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "raw")

    def duplicate_open_interval(rows):
        rows.append(dict(ts_code="000001.SZ", name="Ordinary sample duplicate",
                         start_date="20181220", end_date="", ann_date="20181220", change_reason="其他"))

    _rewrite_namechange(raw, duplicate_open_interval)
    rows = _build_state(raw, tmp_path / "middle")
    row = next(r for r in rows if r["ts_code"] == "000001.SZ" and r["trade_date"] == date(2019, 1, 10))
    assert row["is_st"] is False
    assert row["historical_state_status"] == "namechange_derived"


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


def test_future_namechange_revision_does_not_change_earlier_rows(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "future_raw")

    def replace_future(rows):
        rows[:] = [row for row in rows
                   if not (row["ts_code"] == "000002.SZ" and row["start_date"] == "20190116")]
        rows.append(dict(ts_code="000002.SZ", name="Ordinary sample two",
                         start_date="20190116", end_date="20190131",
                         ann_date="20190116", change_reason="其他"))
        rows.append(dict(ts_code="000002.SZ", name="ST sample future",
                         start_date="20190201", end_date="",
                         ann_date="20190201", change_reason="其他"))

    _rewrite_namechange(raw, replace_future)
    before = _build_state(golden_root, tmp_path / "before")
    after = _build_state(raw, tmp_path / "after")
    cutoff = date(2019, 2, 1)
    # Provenance pointers may move with the source layout; the published state
    # values themselves must not change for earlier dates.
    volatile = ("name_source_file", "name_source_row")
    strip = lambda rows: [{k: v for k, v in row.items() if k not in volatile} for row in rows]
    assert [row for row in strip(before) if row["trade_date"] < cutoff] == [
        row for row in strip(after) if row["trade_date"] < cutoff
    ]
    changed_day = next(row for row in after
                       if row["ts_code"] == "000002.SZ" and row["trade_date"] == cutoff)
    assert changed_day["is_st"] is True


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
    first = old[0]
    assert first["listing_natural_days"] == 715
    assert first["estimated_listing_trade_days"] == round(715 / 365.25 * 252)


def test_natural_days_and_estimated_trade_days_follow_listing_span(tmp_path, golden_root):
    rows = _build_state(golden_root, tmp_path / "middle")
    indexed = {(row["ts_code"], row["trade_date"]): row for row in rows}
    ipo = indexed[("000003.SZ", date(2019, 1, 9))]
    assert ipo["listing_natural_days"] == 0
    assert ipo["estimated_listing_trade_days"] == 0
    listed = indexed[("000001.SZ", date(2019, 1, 11))]
    assert listed["listing_natural_days"] == 25
    assert listed["estimated_listing_trade_days"] == round(25 / 365.25 * 252)
    before = indexed[("000003.SZ", date(2019, 1, 8))]
    assert before["listing_natural_days"] == 0
    after_delist = indexed[("600001.SH", date(2019, 1, 31))]
    assert after_delist["listing_natural_days"] == 36
    assert after_delist["estimated_listing_trade_days"] == round(36 / 365.25 * 252)


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
    assert all(row["is_st"] is None for row in old)
    assert all(row["historical_state_status"] == "not_listed" for row in old)


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


@pytest.mark.parametrize("name,start,end", [
    ("", "20181217", ""),
    ("Valid name", "20190230", ""),
    ("Valid name", "20181217", "20181216"),
    ("Valid name", "", ""),
])
def test_invalid_namechange_rows_fail_before_publication(tmp_path, golden_root, name, start, end):
    raw = _copy_identity_sources(golden_root, tmp_path / "raw")

    def break_row(rows):
        rows.append(dict(ts_code="000001.SZ", name=name, start_date=start,
                         end_date=end, ann_date=start, change_reason="其他"))

    _rewrite_namechange(raw, break_row)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        with pytest.raises(ValueError, match="namechange"):
            build_stock_state(store)
        assert not store.paths.output("l1", "daily_stock_state", "part-00000.parquet").exists()


def test_duplicate_namechange_key_fails_before_publication(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "raw")

    def duplicate(rows):
        rows.append(dict(ts_code="000001.SZ", name="Duplicate start",
                         start_date="20181217", end_date="", ann_date="20181217", change_reason="其他"))

    _rewrite_namechange(raw, duplicate)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        with pytest.raises(ValueError, match="duplicate"):
            build_stock_state(store)
        assert not store.paths.output("l1", "daily_stock_state", "part-00000.parquet").exists()


def test_unmatched_namechange_codes_are_reported_not_dropped(tmp_path, golden_root):
    raw = _copy_identity_sources(golden_root, tmp_path / "raw")

    def add_foreign(rows):
        rows.append(dict(ts_code="688837.SH", name="Retired foreign code",
                         start_date="20181217", end_date="", ann_date="20181217", change_reason="其他"))

    _rewrite_namechange(raw, add_foreign)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        build_calendar(store)
        build_stock_state(store)
        import json
        diagnostics = json.loads(
            store.paths.output("l1", "daily_stock_state", "source_diagnostics.json").read_text(encoding="utf-8"))
        assert diagnostics["namechange_unmatched_codes"]["count"] == 1
        assert diagnostics["namechange_unmatched_codes"]["samples"][0]["ts_code"] == "688837.SH"
