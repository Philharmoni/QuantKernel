"""Independent announcement-time and source-version checks on tiny artificial data."""
import csv
from datetime import date, datetime
import json
import shutil

import pytest

from core.calendar import build_calendar
from core.config import Paths
from core.data.store import Store
from core.financial.available import build_financial_available, financial_asof


FINANCIAL_TABLES = ("income_vip", "cashflow_vip", "balancesheet_vip", "fina_indicator")


def _copy_sources(golden_root, destination):
    destination.mkdir()
    for table in ("trade_cal", *FINANCIAL_TABLES):
        shutil.copy2(golden_root / f"{table}.csv", destination / f"{table}.csv")
    return destination


def _change_table(raw, table, change):
    path = raw / f"{table}.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _all_rows(store):
    relation = store.output_relation("l1", "financial_available")
    return store.rows(f"SELECT * FROM {relation} ORDER BY record_id")


def _asof_rows(store, trade_date):
    return store.rows(f"SELECT * FROM {financial_asof(store, trade_date)} ORDER BY record_id")


def _build(store):
    build_calendar(store)
    build_financial_available(store)


def _original_first_income(rows):
    return next(
        row for row in rows
        if row["source_table"] == "income_vip"
        and row["ts_code"] == "000001.SZ" and row["end_date"] == date(2018, 3, 31)
    )


def test_golden_announcements_and_weekends_use_next_trading_date(tmp_path, golden_root):
    expected = json.loads((golden_root / "expected.json").read_text(encoding="utf-8"))
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _build(store)
        rows = _all_rows(store)
        assert len(rows) == len({row["record_id"] for row in rows}) == 32
        assert {row["source_table"] for row in rows} == set(FINANCIAL_TABLES)
        for case in expected["financial_available_cases"]:
            end_date = datetime.strptime(case["end_date"], "%Y%m%d").date()
            ann_date = datetime.strptime(case["ann_date"], "%Y%m%d").date()
            available = datetime.strptime(case["available_date"], "%Y%m%d").date()
            for table in FINANCIAL_TABLES:
                row = next(
                    row for row in rows
                    if row["source_table"] == table and row["ts_code"] == case["ts_code"]
                    and row["end_date"] == end_date and row["ann_date"] == ann_date
                )
                assert row["available_date"] == available
                assert row["effective_ann_date"] == ann_date
                assert row["availability_status"] == "available"
                assert row["source_table"] == table
                assert row["source_file"] == f"{table}.csv"
                assert row["source_count"] == 1
                assert row["raw_ann_date"] == case["ann_date"]
                assert row["raw_end_date"] == case["end_date"]
                assert row["version"] is not None
                assert row["record_id"] not in {
                    visible["record_id"] for visible in _asof_rows(store, case["invisible_on"])
                }
                assert row["record_id"] in {
                    visible["record_id"] for visible in _asof_rows(store, case["available_date"])
                }
        indicator = next(row for row in rows if row["source_table"] == "fina_indicator")
        assert indicator["f_ann_date"] is None
        assert indicator["raw_f_ann_date"] is None
        relation = store.output_relation("l1", "financial_available")
        types = store.columns(relation)
        for field in ("ann_date", "f_ann_date", "end_date", "available_date", "effective_ann_date"):
            assert types[field] == "DATE"
        assert {"revenue", "n_income_attr_p", "n_cashflow_act", "total_assets", "eps", "report_type", "update_flag"} <= types.keys()


def test_later_actual_announcement_takes_precedence_over_earlier_ann_date(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "income_vip", lambda rows: rows[0].__setitem__("f_ann_date", "20181222"))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        row = _original_first_income(_all_rows(store))
        assert row["ann_date"] == date(2018, 12, 20)
        assert row["f_ann_date"] == date(2018, 12, 22)
        assert row["effective_ann_date"] == date(2018, 12, 22)
        assert row["available_date"] == date(2018, 12, 24)
        assert row["record_id"] not in {r["record_id"] for r in _asof_rows(store, "20181221")}
        assert row["record_id"] in {r["record_id"] for r in _asof_rows(store, "20181224")}


@pytest.mark.parametrize("field,value", [
    ("ann_date", "20190230"), ("f_ann_date", "20190230"),
    ("ann_date", "None"), ("f_ann_date", "bad-date"),
])
def test_nonempty_invalid_announcement_is_preserved_but_never_visible(tmp_path, golden_root, field, value):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "income_vip", lambda rows: rows[0].__setitem__(field, value))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        row = _original_first_income(_all_rows(store))
        assert row[f"raw_{field}"] == value
        assert row[field] is None
        assert row["available_date"] is None
        assert row["availability_status"] != "available"
        assert row["record_id"] not in {r["record_id"] for r in _asof_rows(store, "20190201")}


@pytest.mark.parametrize("report_date,status", [
    ("20190230", "invalid_report_date"),
    ("20190331", "announcement_before_report"),
])
def test_invalid_or_future_report_period_is_not_made_visible(tmp_path, golden_root, report_date, status):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "income_vip", lambda rows: rows[0].__setitem__("end_date", report_date))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        row = next(
            row for row in _all_rows(store)
            if row["source_table"] == "income_vip" and row["source_row"] == 0
        )
        assert row["raw_end_date"] == report_date
        assert row["available_date"] is None
        assert row["availability_status"] == status
        assert row["record_id"] not in {r["record_id"] for r in _asof_rows(store, "20190201")}


@pytest.mark.parametrize("missing", ["ann_date", "f_ann_date", "both"])
def test_missing_date_uses_valid_other_date_without_inventing_an_announcement(tmp_path, golden_root, missing):
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def clear_date(rows):
        for field in ("ann_date", "f_ann_date"):
            if missing in {field, "both"}:
                rows[0][field] = ""

    _change_table(raw, "income_vip", clear_date)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        row = _original_first_income(_all_rows(store))
        if missing == "both":
            assert row["effective_ann_date"] is None
            assert row["available_date"] is None
            assert row["availability_status"] != "available"
        else:
            assert row["effective_ann_date"] == date(2018, 12, 20)
            assert row["available_date"] == date(2018, 12, 21)


@pytest.mark.parametrize("announcement,status", [
    ("20181216", "calendar_before_coverage"),
    ("20190201", "calendar_after_coverage"),
    ("20190202", "calendar_after_coverage"),
    ("20190220", "calendar_after_coverage"),
])
def test_out_of_coverage_announcements_are_not_clamped_to_calendar_endpoints(tmp_path, golden_root, announcement, status):
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def set_dates(rows):
        rows[0].update(ann_date=announcement, f_ann_date=announcement)

    _change_table(raw, "income_vip", set_dates)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        row = _original_first_income(_all_rows(store))
        assert row["available_date"] is None
        assert row["availability_status"] == status
        assert row["record_id"] not in {r["record_id"] for r in _asof_rows(store, "20190201")}


def test_identical_raw_records_are_grouped_without_losing_source_count(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "income_vip", lambda rows: rows.append(dict(rows[0])))
    with Store(Paths(golden_root, tmp_path / "before")) as store:
        _build(store)
        original = _original_first_income(_all_rows(store))
    with Store(Paths(raw, tmp_path / "after")) as store:
        _build(store)
        rows = _all_rows(store)
        duplicate = _original_first_income(rows)
        assert len(rows) == 32
        assert sum(row["source_count"] for row in rows) == 33
        assert duplicate["record_id"] == original["record_id"]
        assert duplicate["version"] == original["version"]
        assert duplicate["source_count"] == 2
        assert duplicate["source_file"] == "income_vip.csv"
        assert duplicate["source_row"] == 0
        assert {(source["file"], source["row"]) for source in duplicate["source_locations"]} == {
            ("income_vip.csv", 0), ("income_vip.csv", 8),
        }


def test_same_candidate_key_different_record_values_are_both_retained(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "income_vip", lambda rows: rows.append(dict(rows[0], revenue="11")))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        rows = _all_rows(store)
        conflicts = [
            row for row in rows if row["source_table"] == "income_vip"
            and row["ts_code"] == "000001.SZ" and row["end_date"] == date(2018, 3, 31)
        ]
        assert len(rows) == 33
        assert len(conflicts) == 2
        assert len({row["record_id"] for row in conflicts}) == 2
        assert {float(row["revenue"]) for row in conflicts} == {10, 11}
        assert all(row["source_count"] == 1 for row in conflicts)
        visible_ids = {row["record_id"] for row in _asof_rows(store, "20181221")}
        assert {row["record_id"] for row in conflicts} <= visible_ids


def test_future_record_does_not_change_previously_visible_rows(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "future_raw")

    def add_future_version(rows):
        rows.append(dict(rows[1], ann_date="20190131", f_ann_date="20190131", revenue="40", update_flag="1"))

    _change_table(raw, "income_vip", add_future_version)
    with Store(Paths(golden_root, tmp_path / "before")) as store:
        _build(store)
        before = _asof_rows(store, "20190115")
    with Store(Paths(raw, tmp_path / "after")) as store:
        _build(store)
        assert before == _asof_rows(store, "20190115")
        assert len(_asof_rows(store, "20190201")) == len(before) + 1


def test_record_identity_survives_source_row_reordering(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "reordered_raw")
    _change_table(raw, "income_vip", lambda rows: rows.reverse())

    def identities(store):
        return {
            row["record_id"]: (row["version"], row["source_table"], row["end_date"], row["available_date"])
            for row in _all_rows(store)
        }

    with Store(Paths(golden_root, tmp_path / "before")) as store:
        _build(store)
        before = identities(store)
    with Store(Paths(raw, tmp_path / "after")) as store:
        _build(store)
        assert identities(store) == before


def test_historical_code_absent_from_stock_basic_is_not_dropped(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    assert not (raw / "stock_basic.csv").exists()
    _change_table(raw, "income_vip", lambda rows: rows[0].__setitem__("ts_code", "830099.BJ"))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        rows = _asof_rows(store, "20181221")
        historical = [row for row in rows if row["ts_code"] == "830099.BJ"]
        assert len(historical) == 1
        assert historical[0]["source_table"] == "income_vip"
        assert float(historical[0]["revenue"]) == 10


def test_nonempty_invalid_security_code_is_preserved_but_excluded_from_asof(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "income_vip", lambda rows: rows[0].__setitem__("ts_code", "INVALID CODE"))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        rows = _all_rows(store)
        assert len(rows) == 32
        invalid = next(row for row in rows if row["ts_code"] == "INVALID CODE")
        assert invalid["source_table"] == "income_vip"
        assert invalid["source_file"] == "income_vip.csv"
        assert invalid["source_row"] == 0
        assert invalid["availability_status"] == "invalid_code"
        assert invalid["available_date"] is None
        assert invalid["record_id"] not in {row["record_id"] for row in _asof_rows(store, "20190201")}
