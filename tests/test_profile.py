import csv

from core.config import Paths
from core.data.profile import profile_table
from core.data.store import Store


def test_profile_reports_duplicates_invalid_dates_and_nulls(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    with (raw / "stk_factor_pro.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ["ts_code", "trade_date", "open", "high", "low", "close", "adj_factor", "vol", "amount"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for date in ("20190102", "20190102", "20190230"):
            writer.writerow(dict(ts_code="000001.SZ", trade_date=date, open=1, high=1, low=1, close=1, adj_factor=1, vol=1))
    with Store(Paths(raw, tmp_path / "middle")) as store:
        report = profile_table(store, "stk_factor_pro")
    assert report["rows"] == 3
    assert report["duplicate_rows"] == 1
    assert report["dates"]["trade_date"]["invalid_count"] == 1
    assert report["columns"]["amount"]["null_rate"] == 1
    assert {issue["kind"] for issue in report["issues"]} == {"duplicate_key", "invalid_date"}


def test_golden_profiles_preserve_source_names(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        report = profile_table(store, "income_vip")
        source = store.db.execute(f"SELECT DISTINCT _source_file FROM {store.raw('income_vip')}").fetchall()
        assert source == [("income_vip.csv",)]
    assert report["rows"] == 8
    assert {"ts_code", "end_date", "ann_date", "f_ann_date", "revenue"} <= report["columns"].keys()


def test_missing_configured_fields_are_explicit(tmp_path):
    import pytest

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "stk_factor_pro.csv").write_text("ts_code,trade_date\n000001.SZ,20190102\n", encoding="utf-8")
    with Store(Paths(raw, tmp_path / "middle")) as store:
        with pytest.raises(ValueError, match="stk_factor_pro: missing configured fields"):
            profile_table(store, "stk_factor_pro")
