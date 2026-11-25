"""Step 5 baseline factor library: every registered factor's golden value (or
required absence) in tests/fixtures/golden_stage2/expected.json's "factors"
section, plus unit conversion, valid_from, key uniqueness and the full-date
truncation-invariance gate.
"""
from datetime import datetime
import json
import math
import shutil

import pytest
import yaml

from core.calendar import build_calendar
from core.calendar.trading import TradingCalendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store, qs
from core.factor.baseline import build_factor_values
from core.factor.entry import check_factor_label_isolation, check_no_raw_access
from core.factor.registry import load_factor_registry
from core.factor.report import build_factor_coverage_report, build_vendor_comparison
from core.factor.truncate import check_truncation_invariance
from core.financial.available import build_financial_available
from core.financial.quarters import build_quarterly_and_ttm
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status

ALL_FACTOR_IDS = ["log_total_mv", "log_circ_mv", "ep_ttm", "sp_ttm", "cfp_ttm", "bp",
                  "mom_12_1", "mom_6_1", "rev_20", "vol_20", "vol_60", "turn_20",
                  "amihud_20", "log_amount_20", "rev_yoy_q", "np_yoy_q", "roa_ttm", "roe_ttm"]
COMPOSITE_FACTOR_IDS = ["composite_equal", "composite_icir_60"]


def _parse(value):
    return datetime.strptime(value, "%Y%m%d").date()


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def built_store(tmp_path_factory):
    golden_stage2_root = PROJECT_ROOT / "tests" / "fixtures" / "golden_stage2"
    tmp_path = tmp_path_factory.mktemp("factor_baseline")
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
    with Store(Paths(golden_stage2_root, tmp_path / "middle"), config_root=destination) as store:
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
        build_trade_status(store)
        build_financial_available(store)
        build_quarterly_and_ttm(store)
        build_factor_values(store)
        yield store


def _value(store, ts_code, trade_date, factor_id):
    if len(trade_date) == 8 and "-" not in trade_date:
        trade_date = _parse(trade_date).isoformat()
    relation = store.output_relation("l3", "factor_value")
    rows = store.rows(f"SELECT value FROM {relation} WHERE ts_code={qs(ts_code)} "
                      f"AND trade_date=DATE {qs(trade_date)} AND factor_id={qs(factor_id)}")
    assert len(rows) <= 1, (ts_code, trade_date, factor_id, rows)
    return rows[0]["value"] if rows else None


def test_registry_has_all_baseline_factors_with_complete_metadata():
    raw = yaml.safe_load((PROJECT_ROOT / "configs" / "factor_registry.yaml").read_text(encoding="utf-8"))
    assert "factors" not in raw and "registry_version" not in raw
    assert list(raw) == ALL_FACTOR_IDS + COMPOSITE_FACTOR_IDS
    registry = load_factor_registry()
    ids = [f["factor_id"] for f in registry["factors"]]
    assert ids == ALL_FACTOR_IDS + COMPOSITE_FACTOR_IDS
    for factor in registry["factors"]:
        assert factor["version"] == 1
        assert factor["valid_from"] == "20180601"
        assert factor["category"] in ("size", "value", "momentum", "volatility", "liquidity",
                                      "growth", "quality", "composite")


def test_golden_factor_values_and_required_absences(built_store, expected):
    cases = expected["factors"]
    for name, case in cases.items():
        if not isinstance(case, dict) or "trade_date" not in case:
            continue
        ts_code = name.split("_at_")[0] + ".SZ"
        for factor_id, wanted in case.get("values", {}).items():
            actual = _value(built_store, ts_code, case["trade_date"], factor_id)
            assert actual is not None, (name, factor_id, "missing row")
            assert actual == pytest.approx(wanted, rel=1e-9, abs=1e-15), (name, factor_id, actual, wanted)
        for factor_id in case.get("absent", []):
            assert _value(built_store, ts_code, case["trade_date"], factor_id) is None, (name, factor_id)


def test_market_cap_unit_conversion_is_yuan(built_store):
    # total_mv = 100 * 10000 = 1,000,000 万元; log factor must use 元 (x10000),
    # i.e. ln(1e10), not ln(1e6). Guards the 万元->元 conversion explicitly.
    assert _value(built_store, "SA001.SZ", "20200323", "log_total_mv") == pytest.approx(math.log(1e10))
    assert _value(built_store, "SA001.SZ", "20200323", "log_amount_20") is None
    assert _value(built_store, "SA001.SZ", "20200330", "log_amount_20") == pytest.approx(math.log(1e7))


def test_factor_key_uniqueness_and_valid_from(built_store):
    relation = built_store.output_relation("l3", "factor_value")
    duplicates = built_store.rows(f"""SELECT trade_date, ts_code, factor_id, count(*) AS n
        FROM {relation} GROUP BY 1,2,3 HAVING count(*)>1 LIMIT 5""")
    assert duplicates == []
    early = built_store.rows(f"SELECT count(*) AS n FROM {relation} WHERE trade_date<DATE '2018-06-01'")
    assert early[0]["n"] == 0
    ids = {r["factor_id"] for r in built_store.rows(f"SELECT DISTINCT factor_id FROM {relation}")}
    # mom_12_1/mom_6_1 can never be computed on a 66-day fixture; all other 16
    # factors must appear somewhere.
    assert ids == set(ALL_FACTOR_IDS) - {"mom_12_1", "mom_6_1"}


def test_rolling_windows_do_not_compress_suspensions(built_store, expected):
    # Explicitly asserted again here (also covered by the golden table): SA003's
    # two suspended days inside the window must reduce the observation count.
    case = expected["factors"]["SA003_at_index35_absent"]
    for factor_id in case["absent"]:
        assert _value(built_store, "SA003.SZ", case["trade_date"], factor_id) is None
    # index60's window (index41..60) excludes both SA003 suspensions (index16
    # and index35), so its 20-day turnover mean exists again.
    calendar = TradingCalendar.from_store(built_store)
    late_date = calendar.trade_dates[60].isoformat()
    assert _value(built_store, "SA003.SZ", late_date, "turn_20") == pytest.approx(2.0)


def test_truncation_invariance_on_every_fixture_date(built_store):
    calendar = TradingCalendar.from_store(built_store)
    report = check_truncation_invariance(built_store, calendar.trade_dates, factor_ids=ALL_FACTOR_IDS)
    assert report["compared_values"] > 0
    assert report["mismatches"] == [], report["mismatches"][:5]
    assert report["passed"] is True
    compact = check_truncation_invariance(
        built_store, [calendar.trade_dates[-1].strftime("%Y%m%d")], factor_ids=["rev_20"])
    assert compact["passed"] and compact["compared_values"] > 0


def test_coverage_and_vendor_reports_on_fixture(built_store):
    coverage = build_factor_coverage_report(built_store)
    assert {row["factor_id"] for row in coverage["summary"]} == set(ALL_FACTOR_IDS + COMPOSITE_FACTOR_IDS)
    daily_path = built_store.paths.output("l3", "factor_value", "coverage_daily.parquet")
    assert daily_path.is_file()
    vendor = build_vendor_comparison(built_store)
    ep = vendor["comparisons"]["ep_ttm"]
    bp = vendor["comparisons"]["bp"]
    assert ep["n"] > 0 and bp["n"] > 0
    # SA001's ep_ttm=0.05 vs vendor 0.04 -> 25% relative deviation, deliberately
    # above the 5% large-deviation threshold; bp matches its vendor reciprocal.
    assert ep["large_deviation_count"] > 0
    assert bp["median_abs_rel_dev"] == pytest.approx(0.0, abs=1e-12)


def test_factor_module_isolation_guards():
    assert check_no_raw_access() == []
    assert check_factor_label_isolation() == []
