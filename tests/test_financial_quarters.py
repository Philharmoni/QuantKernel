"""Hand-calculated quarter/TTM oracles and point-in-time revision dependencies."""
import csv
from datetime import date
import shutil

import pytest
import yaml

from core.calendar import build_calendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store
from core.financial.available import build_financial_available
from core.financial.quarters import build_quarterly_and_ttm, financial_derived_asof


def _copy_sources(golden_root, destination):
    destination.mkdir()
    for table in ("trade_cal", "income_vip", "cashflow_vip", "balancesheet_vip", "fina_indicator"):
        shutil.copy2(golden_root / f"{table}.csv", destination / f"{table}.csv")
    return destination


def _change_income(raw, change):
    path = raw / "income_vip.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _build(store):
    build_calendar(store)
    build_financial_available(store)
    return build_quarterly_and_ttm(store)


def _asof(store, table, day):
    relation = financial_derived_asof(store, table, day)
    return store.rows(f"SELECT * FROM {relation} ORDER BY source_table,ts_code,metric,end_date")


def _metric_rows(store, table, day, code="000001.SZ", source="income_vip", metric="revenue"):
    return {
        row["end_date"]: row for row in _asof(store, table, day)
        if row["source_table"] == source and row["ts_code"] == code and row["metric"] == metric
    }


def test_manual_quarter_and_ttm_oracles_before_and_after_revision(tmp_path, golden_root):
    periods = [date(2018, 3, 31), date(2018, 6, 30), date(2018, 9, 30), date(2018, 12, 31)]
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        manifests = _build(store)
        assert set(manifests) == {"quarterly", "ttm"}
        for day, expected in [("20190107", [10, 15, 30, 45]),
                              ("20190111", [10, 15, 30, 45]),
                              ("20190114", [10, 17, 28, 45])]:
            quarters = _metric_rows(store, "quarterly_financial", day)
            assert [quarters[period]["value"] for period in periods] == expected
            ttm = _metric_rows(store, "ttm_financial", day)[periods[-1]]
            assert ttm["value"] == 100
            assert ttm["source_end_dates"] == periods
            assert ttm["source_quarter_ids"] == [quarters[period]["version_id"] for period in periods]
            assert len(ttm["source_record_ids"]) == 4
            assert all(quarters[period]["source_record_ids"] for period in periods)
        earlier = _metric_rows(store, "ttm_financial", "20190104")
        assert periods[-1] not in earlier or earlier[periods[-1]]["value"] is None


def test_missing_quarter_is_not_substituted_with_an_older_cumulative_value(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _build(store)
        quarters = _metric_rows(store, "quarterly_financial", "20190114", code="000002.SZ")
        assert quarters[date(2018, 3, 31)]["value"] == 10
        third = quarters[date(2018, 9, 30)]
        assert third["value"] is None
        assert "missing" in third["status"].lower()
        assert quarters[date(2018, 12, 31)]["value"] == 45
        ttm = _metric_rows(store, "ttm_financial", "20190114", code="000002.SZ")[date(2018, 12, 31)]
        assert ttm["value"] is None
        assert "missing" in ttm["status"].lower()


def test_only_explicit_cumulative_metrics_are_derived(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _build(store)
        for table in ("quarterly_financial", "ttm_financial"):
            rows = _asof(store, table, "20190114")
            assert {(row["source_table"], row["metric"]) for row in rows} == {
                ("income_vip", "revenue"), ("income_vip", "n_income_attr_p"),
                ("cashflow_vip", "n_cashflow_act"),
            }
            keys = [(row["source_table"], row["ts_code"], row["metric"], row["end_date"]) for row in rows]
            assert len(keys) == len(set(keys))
        for source, metric in [("income_vip", "n_income_attr_p"), ("cashflow_vip", "n_cashflow_act")]:
            ttm = _metric_rows(store, "ttm_financial", "20190114", source=source, metric=metric)
            assert ttm[date(2018, 12, 31)]["value"] == 100


def test_new_year_q1_resets_and_ttm_uses_four_consecutive_cross_year_quarters(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def add_prior_year(rows):
        template = rows[0]
        for period, announcement, cumulative in [
            ("20170331", "20181218", 2), ("20170630", "20181219", 5),
            ("20170930", "20181220", 11), ("20171231", "20181221", 20),
        ]:
            rows.append(dict(template, end_date=period, ann_date=announcement, f_ann_date=announcement,
                             revenue=str(cumulative), n_income_attr_p=str(cumulative)))

    _change_income(raw, add_prior_year)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        quarters = _metric_rows(store, "quarterly_financial", "20181224")
        assert quarters[date(2017, 12, 31)]["value"] == 9
        assert quarters[date(2018, 3, 31)]["value"] == 10
        ttm = _metric_rows(store, "ttm_financial", "20181224")
        assert ttm[date(2018, 3, 31)]["value"] == 28  # 3 + 6 + 9 + 10
        assert ttm[date(2018, 3, 31)]["source_end_dates"] == [
            date(2017, 6, 30), date(2017, 9, 30), date(2017, 12, 31), date(2018, 3, 31),
        ]
        assert ttm[date(2018, 6, 30)]["value"] == 40  # 6 + 9 + 10 + 15


@pytest.mark.parametrize("conflicting_value", ["11", ""])
def test_same_announcement_metric_conflicts_include_null_and_never_use_update_flag(tmp_path, golden_root, conflicting_value):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_income(raw, lambda rows: rows.append(dict(rows[0], revenue=conflicting_value, update_flag="1")))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        revenue = _metric_rows(store, "quarterly_financial", "20190107")
        first = revenue[date(2018, 3, 31)]
        assert first["value"] is None
        assert "ambiguous" in first["status"].lower()
        assert len(first["source_record_ids"]) == 2
        assert revenue[date(2018, 6, 30)]["value"] is None
        profit = _metric_rows(store, "quarterly_financial", "20190107", metric="n_income_attr_p")
        assert profit[date(2018, 3, 31)]["value"] == 10
        profit_ttm = _metric_rows(store, "ttm_financial", "20190107", metric="n_income_attr_p")
        assert profit_ttm[date(2018, 12, 31)]["value"] == 100


def test_latest_effective_announcement_wins_within_a_shared_available_day(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def add_weekend_versions(rows):
        template = rows[0]
        rows.append(dict(template, ann_date="20181221", f_ann_date="20181221", revenue="11", update_flag="1"))
        rows.append(dict(template, ann_date="20181222", f_ann_date="20181222", revenue="12", update_flag="0"))

    _change_income(raw, add_weekend_versions)
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        old = _metric_rows(store, "quarterly_financial", "20181221")
        assert old[date(2018, 3, 31)]["value"] == 10
        current = _metric_rows(store, "quarterly_financial", "20181224")
        assert current[date(2018, 3, 31)]["value"] == 12
        assert current[date(2018, 6, 30)]["value"] == 13
        assert current[date(2018, 9, 30)]["value"] == 30


def test_future_revision_changes_dependents_only_after_its_available_date(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "future_raw")
    _change_income(raw, lambda rows: rows.append(dict(
        rows[1], ann_date="20190131", f_ann_date="20190131", revenue="29", update_flag="1"
    )))
    with Store(Paths(golden_root, tmp_path / "before")) as store:
        _build(store)
        before = {table: _asof(store, table, "20190115") for table in ("quarterly_financial", "ttm_financial")}
    with Store(Paths(raw, tmp_path / "after")) as store:
        _build(store)
        for table in before:
            assert _asof(store, table, "20190115") == before[table]
        quarters = _metric_rows(store, "quarterly_financial", "20190201")
        assert quarters[date(2018, 6, 30)]["value"] == 19
        assert quarters[date(2018, 9, 30)]["value"] == 26
        assert _metric_rows(store, "ttm_financial", "20190201")[date(2018, 12, 31)]["value"] == 100


def test_non_cumulative_report_type_does_not_replace_configured_cumulative_data(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_income(raw, lambda rows: rows.append(dict(rows[0], report_type="2", revenue="999", n_income_attr_p="999")))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        first = _metric_rows(store, "quarterly_financial", "20190107")[date(2018, 3, 31)]
        assert first["value"] == 10
        assert _metric_rows(store, "ttm_financial", "20190107")[date(2018, 12, 31)]["value"] == 100


def test_lineage_never_references_financial_records_not_yet_available(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _build(store)
        available = store.output_relation("l1", "financial_available")
        source_dates = {
            row["record_id"]: row["available_date"]
            for row in store.rows(f"SELECT record_id,available_date FROM {available}")
        }
        for table in ("quarterly_financial", "ttm_financial"):
            relation = store.output_relation("l1", table)
            rows = store.rows(f"SELECT available_date,source_record_ids FROM {relation}")
            assert rows
            for row in rows:
                assert all(source_dates[record_id] <= row["available_date"] for record_id in row["source_record_ids"])


def test_asof_helper_only_accepts_financial_derived_tables(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _build(store)
        with pytest.raises(ValueError):
            financial_derived_asof(store, "daily_adjusted_price", "20190114")


def test_metric_configuration_must_belong_to_its_own_source_schema(tmp_path, golden_root):
    config_root = tmp_path / "configs"
    config_root.mkdir()
    for name in ("tushare_tables.yaml", "data_middle_layer.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / name, config_root / name)
    path = config_root / "data_middle_layer.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules["financial"]["cumulative_fields"]["income_vip"] = ["n_cashflow_act"]
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    with Store(Paths(golden_root, tmp_path / "middle"), config_root=config_root) as store:
        build_calendar(store)
        build_financial_available(store)
        with pytest.raises(ValueError, match="income_vip.*n_cashflow_act|n_cashflow_act.*income_vip"):
            build_quarterly_and_ttm(store)


def test_non_quarter_end_is_explicitly_unsupported_and_does_not_contaminate_real_quarters(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_income(raw, lambda rows: rows.append(dict(rows[0], end_date="20180330", revenue="999")))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        _build(store)
        for table in ("quarterly_financial", "ttm_financial"):
            rows = _metric_rows(store, table, "20190107")
            invalid = rows[date(2018, 3, 30)]
            assert invalid["value"] is None
            assert invalid["status"] == "unsupported_report_period"
            assert invalid["source_record_ids"]
        quarters = _metric_rows(store, "quarterly_financial", "20190107")
        assert quarters[date(2018, 3, 31)]["value"] == 10
        assert quarters[date(2018, 6, 30)]["value"] == 15
        assert _metric_rows(store, "ttm_financial", "20190107")[date(2018, 12, 31)]["value"] == 100
