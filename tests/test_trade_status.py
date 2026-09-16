"""Direction-specific trade facts with explicit artificial source coverage."""
import csv
from datetime import date, datetime
import json
import shutil

import pytest
import yaml

from core.calendar import build_calendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status


def _fixture_config(tmp_path, *, complete_limits=True, **overrides):
    destination = tmp_path / "configs"
    destination.mkdir()
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / filename, destination / filename)
    path = destination / "data_middle_layer.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules["trade_status"].update(
        limit_events_complete=complete_limits,
        suspension_events_complete=True,
        suspension_complete_code_suffixes=["SH", "SZ", "BJ"],
        coverage_start="20181217", coverage_end="20190208",
        limit_coverage_start="20181217", limit_coverage_end="20190208",
    )
    rules["trade_status"].update(overrides)
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    return destination


def _copy_sources(golden_root, destination):
    destination.mkdir()
    for table in ("trade_cal", "stock_basic", "stk_factor_pro", "suspend_d", "limit_list_d"):
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


def _build_dependencies(store):
    build_calendar(store)
    build_adjusted_price(store)
    build_stock_state(store)


def _build_rows(raw, middle, config_root, **kwargs):
    with Store(Paths(raw, middle), config_root=config_root) as store:
        _build_dependencies(store)
        build_trade_status(store, **kwargs)
        relation = store.output_relation("l1", "daily_trade_status")
        return store.rows(f"SELECT * FROM {relation} ORDER BY trade_date, ts_code")


def _index(rows):
    return {(row["ts_code"], row["trade_date"]): row for row in rows}


def test_golden_trade_directions_follow_manual_oracle(tmp_path, golden_root):
    config = _fixture_config(tmp_path)
    rows = _build_rows(golden_root, tmp_path / "middle", config)
    indexed = _index(rows)
    assert len(rows) == len(indexed) == 34 * 5
    expected = json.loads((golden_root / "expected.json").read_text(encoding="utf-8"))
    for case in expected["trade_status_cases"]:
        key = (case["ts_code"], datetime.strptime(case["trade_date"], "%Y%m%d").date())
        actual = indexed[key]
        for field, value in case.items():
            if field not in {"ts_code", "trade_date"}:
                assert actual[field] is value, (key, field)

    no_quote = indexed[("000001.SZ", date(2019, 1, 9))]
    assert no_quote["is_suspended"] is False
    assert "NO_QUOTE" in no_quote["cannot_buy_reason"]
    assert "NO_QUOTE" in no_quote["cannot_sell_reason"]
    suspension = indexed[("000001.SZ", date(2019, 1, 3))]
    assert suspension["is_full_day_suspended"] is True
    assert suspension["is_intraday_suspended"] is False
    assert "suspend_d.csv" in repr(suspension["source_suspend_files"])
    up = indexed[("000001.SZ", date(2019, 1, 4))]
    assert "U" in up["observed_limit_events"]
    assert "limit_list_d.csv" in repr(up["source_limit_files"])


def test_intraday_suspension_is_explicit_and_resumption_is_not_suspension(tmp_path, golden_root):
    config = _fixture_config(tmp_path)
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def append_events(rows):
        rows.extend([
            dict(ts_code="000001.SZ", trade_date="20190110", suspend_timing="", suspend_type="R"),
            dict(ts_code="000001.SZ", trade_date="20190111", suspend_timing="09:30-10:00", suspend_type="S"),
        ])

    _change_table(raw, "suspend_d", append_events)
    indexed = _index(_build_rows(raw, tmp_path / "middle", config))
    resumed = indexed[("000001.SZ", date(2019, 1, 10))]
    assert resumed["quotation_exists"] is True
    assert resumed["is_suspended"] is False
    assert resumed["can_buy"] is True
    assert resumed["can_sell"] is True
    intraday = indexed[("000001.SZ", date(2019, 1, 11))]
    assert intraday["quotation_exists"] is True
    assert intraday["is_suspended"] is True
    assert intraday["is_full_day_suspended"] is False
    assert intraday["is_intraday_suspended"] is True
    assert intraday["can_buy"] is False
    assert intraday["can_sell"] is False
    assert "INTRADAY_SUSPENSION" in intraday["cannot_buy_reason"]
    assert "INTRADAY_SUSPENSION" in intraday["cannot_sell_reason"]


def test_incomplete_limits_preserve_unknown_but_observed_directions_remain_useful(tmp_path, golden_root):
    config = _fixture_config(tmp_path, complete_limits=False)
    indexed = _index(_build_rows(golden_root, tmp_path / "middle", config))
    ordinary = indexed[("000001.SZ", date(2019, 1, 2))]
    for field in ("is_limit_up", "is_limit_down", "can_buy", "can_sell"):
        assert ordinary[field] is None
    assert "UNKNOWN" in ordinary["cannot_buy_reason"]
    assert "UNKNOWN" in ordinary["cannot_sell_reason"]
    up = indexed[("000001.SZ", date(2019, 1, 4))]
    assert up["is_limit_up"] is True
    assert up["is_limit_down"] is False
    assert up["can_buy"] is False
    assert up["can_sell"] is True
    down = indexed[("000001.SZ", date(2019, 1, 7))]
    assert down["is_limit_up"] is False
    assert down["is_limit_down"] is True
    assert down["can_buy"] is True
    assert down["can_sell"] is False
    suspended = indexed[("000001.SZ", date(2019, 1, 3))]
    assert suspended["can_buy"] is False
    assert suspended["can_sell"] is False


def test_opened_limit_event_does_not_mean_closed_limit_up(tmp_path, golden_root):
    config = _fixture_config(tmp_path, complete_limits=False)
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "limit_list_d", lambda rows: rows.append(
        dict(trade_date="20190111", ts_code="000001.SZ", close="49.5", limit="Z")
    ))
    indexed = _index(_build_rows(raw, tmp_path / "middle", config))
    opened = indexed[("000001.SZ", date(2019, 1, 11))]
    assert opened["is_limit_up"] is False
    assert opened["is_limit_down"] is None
    assert opened["can_buy"] is True
    assert opened["can_sell"] is None
    assert "Z" in opened["observed_limit_events"]


def test_zero_volume_blocks_both_directions_without_inventing_suspension(tmp_path, golden_root):
    config = _fixture_config(tmp_path)
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def remove_volume(rows):
        next(row for row in rows if row["ts_code"] == "000001.SZ" and row["trade_date"] == "20190111")["vol"] = "0"

    _change_table(raw, "stk_factor_pro", remove_volume)
    indexed = _index(_build_rows(raw, tmp_path / "middle", config))
    illiquid = indexed[("000001.SZ", date(2019, 1, 11))]
    assert illiquid["quotation_exists"] is True
    assert illiquid["is_suspended"] is False
    assert illiquid["can_buy"] is False
    assert illiquid["can_sell"] is False
    assert illiquid["cannot_buy_reason"]
    assert illiquid["cannot_sell_reason"]


def test_missing_suspension_coverage_does_not_imply_no_suspension(tmp_path, golden_root):
    config = _fixture_config(tmp_path, coverage_start="20190104")
    indexed = _index(_build_rows(golden_root, tmp_path / "middle", config))
    uncertain = indexed[("000001.SZ", date(2019, 1, 2))]
    assert uncertain["quotation_exists"] is True
    assert uncertain["is_suspended"] is None
    assert uncertain["can_buy"] is None
    assert uncertain["can_sell"] is None
    assert "UNKNOWN" in uncertain["cannot_buy_reason"]
    assert "UNKNOWN" in uncertain["cannot_sell_reason"]


def test_bj_without_code_mapping_is_unknown_but_matching_suspension_is_known(tmp_path, golden_root):
    config = _fixture_config(tmp_path, suspension_complete_code_suffixes=["SH", "SZ"])
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, "suspend_d", lambda rows: rows.append(
        dict(ts_code="830001.BJ", trade_date="20190110", suspend_timing="", suspend_type="S")
    ))
    indexed = _index(_build_rows(raw, tmp_path / "middle", config))
    unknown = indexed[("830001.BJ", date(2019, 1, 11))]
    assert unknown["quotation_exists"] is True
    assert unknown["is_suspended"] is None
    assert unknown["is_full_day_suspended"] is None
    assert unknown["is_intraday_suspended"] is None
    assert unknown["can_buy"] is None
    assert unknown["can_sell"] is None
    assert "SUSPENSION" in unknown["cannot_buy_reason"]
    assert "UNKNOWN" in unknown["cannot_buy_reason"]
    observed = indexed[("830001.BJ", date(2019, 1, 10))]
    assert observed["is_suspended"] is True
    assert observed["is_full_day_suspended"] is True
    assert observed["can_buy"] is False
    assert observed["can_sell"] is False
    assert "suspend_d.csv" in repr(observed["source_suspend_files"])
    shenzhen = indexed[("000002.SZ", date(2019, 1, 11))]
    assert shenzhen["is_suspended"] is False
    assert shenzhen["can_buy"] is True
    assert shenzhen["can_sell"] is True


def test_trade_status_slice_is_identical_to_full_output(tmp_path, golden_root):
    config = _fixture_config(tmp_path)
    full = _build_rows(golden_root, tmp_path / "full", config)
    sliced = _build_rows(golden_root, tmp_path / "slice", config, start="20190103", end="20190111")
    assert sliced == [
        row for row in full if date(2019, 1, 3) <= row["trade_date"] <= date(2019, 1, 11)
    ]


@pytest.mark.parametrize("upstream", ["daily_stock_state", "daily_adjusted_price"])
def test_short_upstream_range_cannot_be_used_as_full_coverage(tmp_path, golden_root, upstream):
    config = _fixture_config(tmp_path)
    with Store(Paths(golden_root, tmp_path / "middle"), config_root=config) as store:
        _build_dependencies(store)
        builder = build_stock_state if upstream == "daily_stock_state" else build_adjusted_price
        builder(store, start="20190104", end="20190111")
        with pytest.raises(ValueError, match="coverage"):
            build_trade_status(store)
        assert not store.paths.output("l1", "daily_trade_status", "part-00000.parquet").exists()


@pytest.mark.parametrize("missing_scope", ["whole_day", "one_security"])
def test_interior_state_date_gap_is_rejected_before_omitting_daily_trade_rows(tmp_path, golden_root, missing_scope):
    config = _fixture_config(tmp_path)
    with Store(Paths(golden_root, tmp_path / "middle"), config_root=config) as store:
        _build_dependencies(store)
        relation = store.output_relation("l1", "daily_stock_state")
        keep = "trade_date <> DATE '2019-01-09'"
        if missing_scope == "one_security":
            keep += " OR ts_code <> '000001.SZ'"
        store.publish(
            "l1", "daily_stock_state",
            f"SELECT * FROM {relation} WHERE {keep}",
            ["trade_date", "ts_code"],
        )
        with pytest.raises(ValueError, match="coverage|missing|incomplete"):
            build_trade_status(store, start="20190103", end="20190111")
        assert not store.paths.output("l1", "daily_trade_status", "part-00000.parquet").exists()


@pytest.mark.parametrize("table,field,value", [
    ("suspend_d", "suspend_type", "UNKNOWN"),
    ("limit_list_d", "limit", "UNKNOWN"),
    ("suspend_d", "trade_date", "20190230"),
    ("limit_list_d", "trade_date", "20190230"),
])
def test_invalid_event_codes_and_dates_fail_before_publication(tmp_path, golden_root, table, field, value):
    config = _fixture_config(tmp_path)
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_table(raw, table, lambda rows: rows[0].__setitem__(field, value))
    with Store(Paths(raw, tmp_path / "middle"), config_root=config) as store:
        _build_dependencies(store)
        with pytest.raises(ValueError, match="suspend_d|limit_list_d|event|date"):
            build_trade_status(store)
        assert not store.paths.output("l1", "daily_trade_status", "part-00000.parquet").exists()
