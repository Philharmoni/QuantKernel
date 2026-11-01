"""Step 1 Stage 2 fixture acceptance: run the existing Stage 1 builders over the
golden_stage2 fixture and check the directly-observable facts documented in
README.md / expected.json. No Stage 2 module exists yet; this only validates
the fixture's own internal consistency against the hand-designed scenarios.
"""
from datetime import date, datetime
import json
import shutil

import pytest
import yaml

from core.calendar import build_calendar
from core.calendar.trading import TradingCalendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store, qs
from core.financial.available import build_financial_available
from core.financial.quarters import build_quarterly_and_ttm, financial_derived_asof
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status


Q4_END_DATE = date(2019, 12, 31)


def _parse(value):
    return datetime.strptime(value, "%Y%m%d").date()


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


def _fixture_config(tmp_path):
    # Mirrors tests/test_trade_status.py's own override: without declaring the
    # limit-event source complete, every stock with no explicit U/D/Z event on
    # a given day resolves is_limit_up/is_limit_down (and therefore can_buy/
    # can_sell) to unknown (NULL), not False. The golden_stage2 fixture only
    # carries limit events for SA004's one-word board, so this fixture needs
    # the same declaration the Stage 1 golden fixture tests use.
    destination = tmp_path / "configs"
    destination.mkdir()
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / filename, destination / filename)
    path = destination / "data_middle_layer.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules["trade_status"].update(
        limit_events_complete=True,
        suspension_events_complete=True,
        suspension_complete_code_suffixes=["SH", "SZ", "BJ"],
        coverage_start="20200302", coverage_end="20200605",
        limit_coverage_start="20200302", limit_coverage_end="20200605",
    )
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    return destination


@pytest.fixture
def built_store(tmp_path, golden_stage2_root):
    config = _fixture_config(tmp_path)
    with Store(Paths(golden_stage2_root, tmp_path / "middle"), config_root=config) as store:
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
        build_trade_status(store)
        build_financial_available(store)
        build_quarterly_and_ttm(store)
        yield store


def _trade_status_row(store, ts_code, trade_date):
    relation = store.output_relation("l1", "daily_trade_status")
    rows = store.rows(f"SELECT * FROM {relation} WHERE ts_code={qs(ts_code)} "
                      f"AND trade_date=DATE {qs(trade_date.isoformat())}")
    assert len(rows) == 1, (ts_code, trade_date, rows)
    return rows[0]


def _price_row(store, ts_code, trade_date):
    relation = store.output_relation("l1", "daily_adjusted_price")
    rows = store.rows(f"SELECT * FROM {relation} WHERE ts_code={qs(ts_code)} "
                      f"AND trade_date=DATE {qs(trade_date.isoformat())}")
    return rows[0] if rows else None


def _financial_row(store, source_table, ts_code, end_date, update_flag):
    relation = store.output_relation("l1", "financial_available")
    rows = store.rows(f"SELECT * FROM {relation} WHERE source_table={qs(source_table)} "
                      f"AND ts_code={qs(ts_code)} AND end_date=DATE {qs(end_date.isoformat())} "
                      f"AND update_flag={qs(update_flag)}")
    assert len(rows) == 1, (source_table, ts_code, end_date, update_flag, rows)
    return rows[0]


def _ttm_value(store, trade_date, end_date, metric="n_income_attr_p",
               source="income_vip", code="SA001.SZ"):
    relation = financial_derived_asof(store, "ttm_financial", trade_date)
    rows = store.rows(f"SELECT * FROM {relation} WHERE source_table={qs(source)} "
                      f"AND ts_code={qs(code)} AND metric={qs(metric)} "
                      f"AND end_date=DATE {qs(end_date.isoformat())}")
    return rows[0] if rows else None


def _cumulative_metrics(store):
    fields = store.rules["financial"]["cumulative_fields"]
    return {metric for metrics in fields.values() for metric in metrics}


def test_full_stage2_pipeline_builds_without_error(built_store):
    for layer, name in [
        ("l1", "daily_calendar"), ("l1", "daily_adjusted_price"), ("l1", "daily_stock_state"),
        ("l1", "daily_trade_status"), ("l1", "financial_available"),
        ("l1", "quarterly_financial"), ("l1", "ttm_financial"),
    ]:
        assert built_store.paths.output(layer, name, "part-00000.parquet").is_file()


def test_calendar_has_the_planned_66_trading_days(built_store, expected):
    relation = built_store.output_relation("l1", "daily_calendar")
    count = built_store.db.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
    assert count == expected["calendar"]["trading_dates"] == 66


def test_sa003_full_day_suspension_on_entry_blocks_buying(built_store, expected):
    entry_date = _parse(expected["scenario_indices"]["ENTRY_date"])
    row = _trade_status_row(built_store, "SA003.SZ", entry_date)
    assert row["quotation_exists"] is False
    assert row["is_full_day_suspended"] is True
    assert row["can_buy"] is False
    assert row["cannot_buy_reason"] == "FULL_DAY_SUSPENSION"


def test_sa004_sustained_up_limit_on_entry(built_store, expected):
    entry_date = _parse(expected["scenario_indices"]["ENTRY_date"])
    row = _trade_status_row(built_store, "SA004.SZ", entry_date)
    assert row["is_limit_up"] is True
    assert row["can_buy"] is False
    assert row["cannot_buy_reason"] == "LIMIT_UP"
    price = _price_row(built_store, "SA004.SZ", entry_date)
    assert price["close"] == 104.5


def test_sa006_delisting_blocks_buying_and_ends_quotations(built_store, expected):
    delist_date = _parse(expected["scenario_indices"]["SA006_delist_date"])
    last_quote_date = _parse(expected["scenario_indices"]["SA006_last_quote_date"])
    row = _trade_status_row(built_store, "SA006.SZ", delist_date)
    assert row["is_delisted"] is True
    assert row["quotation_exists"] is False
    assert row["can_buy"] is False
    assert row["cannot_buy_reason"] == "DELISTED"
    last_quote = _price_row(built_store, "SA006.SZ", last_quote_date)
    assert last_quote["close"] == 110
    relation = built_store.output_relation("l1", "daily_adjusted_price")
    later = built_store.db.execute(
        f"SELECT count(*) FROM {relation} WHERE ts_code='SA006.SZ' "
        f"AND trade_date>=DATE {qs(delist_date.isoformat())}").fetchone()[0]
    assert later == 0


def test_sa005_exit_delay_suspension_and_resumption(built_store, expected):
    calendar = TradingCalendar.from_store(built_store)
    exit5_date = _parse(expected["scenario_indices"]["EXIT5_date"])
    exit5_plus1_date = calendar.offset(exit5_date, 1)
    resume_date = _parse(expected["scenario_indices"]["EXIT5_RESUME_date"])
    for suspended_date in (exit5_date, exit5_plus1_date):
        row = _trade_status_row(built_store, "SA005.SZ", suspended_date)
        assert row["is_full_day_suspended"] is True
        assert row["can_buy"] is False
        assert row["cannot_buy_reason"] == "FULL_DAY_SUSPENSION"
    resumed_status = _trade_status_row(built_store, "SA005.SZ", resume_date)
    assert resumed_status["can_buy"] is True
    resumed_price = _price_row(built_store, "SA005.SZ", resume_date)
    assert resumed_price["close"] == 115


def test_sa001_q4_original_and_revision_have_distinct_available_dates(built_store, expected):
    pit = expected["financial_pit"]
    original = _financial_row(built_store, "income_vip", "SA001.SZ", Q4_END_DATE, "0")
    assert original["ann_date"] == _parse(pit["q4_original"]["ann_date"])
    assert original["available_date"] == _parse(pit["q4_original"]["available_date"])
    assert float(original["revenue"]) == pit["q4_original"]["revenue"]
    assert float(original["n_income_attr_p"]) == pit["q4_original"]["n_income_attr_p"]
    assert original["n_cashflow_act"] is None  # union-by-name column; not populated by income_vip rows

    revision = _financial_row(built_store, "income_vip", "SA001.SZ", Q4_END_DATE, "1")
    assert revision["ann_date"] == _parse(pit["q4_revision"]["ann_date"])
    assert revision["available_date"] == _parse(pit["q4_revision"]["available_date"])
    assert float(revision["n_income_attr_p"]) == pit["q4_revision"]["n_income_attr_p"]
    # The revision's available_date is exactly one trading day after the original's.
    calendar = TradingCalendar.from_store(built_store)
    assert revision["available_date"] == calendar.offset(original["available_date"], 1)


def test_sa001_q4_whitelisted_income_fields_raw_and_ttm(built_store, expected):
    # oper_cost/operate_profit/total_profit were added to income_vip's
    # cumulative_fields whitelist in Step 2 (see README.md); check both the raw
    # financial_available passthrough and the derived TTM full-year total
    # (Q1..Q4 2019 single-quarter values sum back to the FY total, same shape
    # as n_income_attr_p's boundary case).
    pit = expected["financial_pit"]["q4_original"]
    row = _financial_row(built_store, "income_vip", "SA001.SZ", Q4_END_DATE, "0")
    assert float(row["oper_cost"]) == pit["oper_cost"]
    assert float(row["operate_profit"]) == pit["operate_profit"]
    assert float(row["total_profit"]) == pit["total_profit"]
    for metric in ("oper_cost", "operate_profit", "total_profit"):
        assert metric in _cumulative_metrics(built_store)

    t_date = _parse(expected["scenario_indices"]["T_date"])
    for metric, total in (("oper_cost", pit["oper_cost"]), ("operate_profit", pit["operate_profit"]),
                          ("total_profit", pit["total_profit"])):
        ttm_row = _ttm_value(built_store, t_date, Q4_END_DATE, metric=metric)
        assert ttm_row["value"] == total


def test_sa001_ttm_net_income_boundary_at_entry(built_store, expected):
    pit = expected["financial_pit"]
    t_date = _parse(expected["scenario_indices"]["T_date"])
    entry_date = _parse(expected["scenario_indices"]["ENTRY_date"])
    before_date = _parse(pit["q4_original"]["ann_date"])

    at_t = _ttm_value(built_store, t_date, Q4_END_DATE)
    assert at_t["value"] == pit["at_T_index15"]["ttm_n_income_attr_p"] == 500_000_000

    at_t_plus1 = _ttm_value(built_store, entry_date, Q4_END_DATE)
    assert at_t_plus1["value"] == pit["at_T_plus1_index16"]["ttm_n_income_attr_p"] == 520_000_000

    before = _ttm_value(built_store, before_date, Q4_END_DATE)
    assert before is None or before["value"] is None


def test_sa001_point_in_time_balance_sheet_fields_are_raw_passthrough(built_store, expected):
    pit = expected["financial_pit"]["point_in_time_balance_at_T"]
    row = _financial_row(built_store, "balancesheet_vip", "SA001.SZ", Q4_END_DATE, "0")
    assert row["available_date"] == _parse(pit["available_date"])
    assert float(row["total_assets"]) == pit["total_assets"]
    assert float(row["total_hldr_eqy_exc_min_int"]) == pit["total_hldr_eqy_exc_min_int"]
    assert float(row["total_liab"]) == pit["total_liab"]
    assert float(row["total_cur_assets"]) == pit["total_cur_assets"]
    assert float(row["total_cur_liab"]) == pit["total_cur_liab"]
    assert float(row["total_assets"]) == float(row["total_liab"]) + float(row["total_hldr_eqy_exc_min_int"])
    # balancesheet_vip fields are documented as point_in_time_fields, not
    # cumulative_fields: no quarterly/TTM derivation exists for them (point_in_time_fields
    # is not consumed by core.financial.quarters, only cumulative_fields is).
    quarterly = built_store.output_relation("l1", "quarterly_financial")
    count = built_store.db.execute(
        f"SELECT count(*) FROM {quarterly} WHERE source_table='balancesheet_vip'").fetchone()[0]
    assert count == 0
