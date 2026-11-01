"""Step 2 industry PIT classification and daily index-constituent expansion:
core.reference.membership (raw ingestion) + core.industry.pit +
core.benchmark.membership built on the golden_stage2 fixture.
"""
from datetime import datetime
import json
import shutil

import pytest
import yaml

from core.benchmark.membership import build_daily_index_member
from core.calendar import build_calendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store, qs
from core.industry.pit import build_daily_industry
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.reference.membership import build_index_member_available, build_index_weight_available


def _parse(value):
    return datetime.strptime(value, "%Y%m%d").date()


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


def _fixture_config(tmp_path):
    # Same trade_status override as tests/test_golden_stage2_fixture.py: the
    # fixture only carries limit events for SA004, so limit_events_complete
    # must be declared True or unrelated is_limit_up/can_buy assertions would
    # resolve to unknown instead of False. Stage 1 builders run here only to
    # produce daily_calendar for the new Stage 2 modules under test.
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
        build_index_member_available(store)
        build_index_weight_available(store)
        build_daily_industry(store)
        build_daily_index_member(store)
        yield store


def _rows(store, layer, name, where):
    relation = store.output_relation(layer, name)
    return store.rows(f"SELECT * FROM {relation} WHERE {where}")


def test_index_member_available_is_raw_passthrough(built_store, expected):
    pit = expected["industry_pit"]
    for ts_code, info in pit.items():
        if ts_code == "description":
            continue
        rows = _rows(built_store, "l1", "index_member_available", f"ts_code={qs(ts_code)}")
        assert len(rows) == 1, (ts_code, rows)
        row = rows[0]
        assert row["l1_code"] == info["l1_code"]
        assert row["in_date"].strftime("%Y%m%d") == info["in_date"]
        assert row["out_date"] is None
        assert row["is_new"] == info["is_new"]


def test_index_weight_available_is_raw_passthrough(built_store, expected):
    # Step 4 expanded the fixture from two to four 000300.SH members (SA003 /
    # SA004 added for the hand-computable benchmark scenario).
    members = expected["benchmark"]["index_weight_members"]["000300.SH"]
    rows = _rows(built_store, "l1", "index_weight_available", "index_code='000300.SH'")
    assert {(r["con_code"], r["weight"]) for r in rows} == {
        (code, weight) for code, weight in members.items()}
    for row in rows:
        assert row["trade_date"].strftime("%Y%m%d") == "20200201"


def test_sa007_industry_unknown_backfilled_then_after_in_date(built_store, expected):
    # SA007's in_date (index30 = 20200414) falls strictly inside the fixture
    # calendar; at T=index15 (20200323, < in_date) the conservative "strict"
    # policy has not yet joined this sector (status=snapshot_backfilled, not
    # yet usable per factor_research.yaml industry.strict_usable_statuses);
    # from in_date onward it is an observed member (snapshot_after_in_date).
    t_date = "2020-03-23"
    in_date = _parse(expected["industry_pit"]["SA007.SZ"]["in_date"]).isoformat()
    before = _rows(built_store, "l3", "daily_industry",
                   f"ts_code='SA007.SZ' AND trade_date=DATE {qs(t_date)}")[0]
    assert before["industry_pit_status"] == "snapshot_backfilled"
    assert before["l1_code"] == "900001.SI"  # value still carried, just flagged backfilled
    on_in_date = _rows(built_store, "l3", "daily_industry",
                       f"ts_code='SA007.SZ' AND trade_date=DATE {qs(in_date)}")[0]
    assert on_in_date["industry_pit_status"] == "snapshot_after_in_date"
    assert on_in_date["l1_code"] == "900001.SI"


def test_sa008_industry_member_on_every_fixture_date(built_store):
    # in_date=20190101 precedes the fixture's first trading date (20200302):
    # SA008 is an observed member on every row, never backfilled/unknown.
    rows = _rows(built_store, "l3", "daily_industry", "ts_code='SA008.SZ'")
    assert rows
    assert {r["industry_pit_status"] for r in rows} == {"snapshot_after_in_date"}
    assert all(r["l1_code"] == "900002.SI" for r in rows)


def test_daily_industry_single_row_per_known_stock_day(built_store):
    counts = built_store.rows("""SELECT trade_date, ts_code, count(*) AS n
        FROM {} GROUP BY trade_date, ts_code HAVING count(*)>1""".format(
        built_store.output_relation("l3", "daily_industry")))
    assert counts == []


def test_daily_index_member_as_of_expansion_respects_publication_lag(built_store):
    relation = built_store.output_relation("l3", "daily_index_member")
    rows = built_store.rows(f"SELECT * FROM {relation} WHERE index_code='000300.SH' "
                            f"AND con_code='SA001.SZ' ORDER BY trade_date")
    assert rows
    first = rows[0]
    # weight_trade_date=20200201 anchors to the fixture calendar's first
    # trading date (20200302, since 20200201 itself precedes calendar_start)
    # and the 5-trading-day publication lag pushes availability to 20200309.
    assert first["trade_date"].strftime("%Y%m%d") == "20200309"
    assert first["available_date"].strftime("%Y%m%d") == "20200309"
    assert first["weight"] == 5.0
    before = built_store.rows(f"SELECT * FROM {relation} WHERE index_code='000300.SH' "
                              f"AND con_code='SA001.SZ' AND trade_date<DATE '2020-03-09'")
    assert before == []


def test_daily_index_member_sa002_weight_and_no_rebalances_dropped(built_store):
    relation = built_store.output_relation("l3", "daily_index_member")
    rows = built_store.rows(f"SELECT DISTINCT weight FROM {relation} "
                            f"WHERE index_code='000300.SH' AND con_code='SA002.SZ'")
    assert [r["weight"] for r in rows] == [3.0]
    diagnostics = json.loads(built_store.paths.output(
        "l3", "daily_index_member", "source_diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["publication_lag_trading_days"] == 5
    assert diagnostics["rebalances_dropped_beyond_calendar_coverage"] == []
