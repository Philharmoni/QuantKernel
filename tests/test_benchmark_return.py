"""Step 4 self-built benchmark returns and official-index calibration:
core.reference.market (index_daily ingestion) + core.benchmark.returns +
core.benchmark.calibration, against the hand-computed expectations in
tests/fixtures/golden_stage2/expected.json's "benchmark" section.
"""
from datetime import datetime
import json
import math
import shutil

import pytest
import yaml

from core.benchmark.calibration import build_benchmark_calibration, calibration_metrics
from core.benchmark.membership import build_daily_index_member
from core.benchmark.returns import build_daily_benchmark_return
from core.calendar import build_calendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store, qs
from core.factor.entry import check_no_raw_access
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.reference.market import build_index_daily_available
from core.reference.membership import build_index_weight_available
from core.universe.research import build_research_universe


def _parse(value):
    return datetime.strptime(value, "%Y%m%d").date()


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def built_store(tmp_path, golden_stage2_root):
    destination = tmp_path / "configs"
    destination.mkdir()
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml", "factor_research.yaml"):
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
    # Point the calibration at the fixture's own 000300.SH official series; the
    # real 000852.SH comparison only exists on the full data. The fixture has 4
    # overlapping days, so the production min_overlap_days=20 must shrink too.
    research_path = destination / "factor_research.yaml"
    research = yaml.safe_load(research_path.read_text(encoding="utf-8"))
    research["benchmark"]["calibration"].update(
        benchmark_id="index_member_hs300", official_index_code="000300.SH", min_overlap_days=4)
    research_path.write_text(yaml.safe_dump(research, allow_unicode=True), encoding="utf-8")
    with Store(Paths(golden_stage2_root, tmp_path / "middle"), config_root=destination) as store:
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
        build_trade_status(store)
        build_research_universe(store)
        build_index_weight_available(store)
        build_daily_index_member(store)
        build_index_daily_available(store)
        build_daily_benchmark_return(store, config_root=destination)
        yield store, destination


def _benchmark_row(store, trade_date, method, benchmark_id="index_member_hs300"):
    relation = store.output_relation("l3", "daily_benchmark_return")
    rows = store.rows(f"SELECT * FROM {relation} WHERE benchmark_id={qs(benchmark_id)} "
                      f"AND method={qs(method)} AND trade_date=DATE {qs(_parse(trade_date).isoformat())}")
    assert len(rows) == 1, (trade_date, method, rows)
    return rows[0]


def test_index_daily_available_is_raw_passthrough(built_store, expected):
    store, _ = built_store
    relation = store.output_relation("l1", "index_daily_available")
    rows = store.rows(f"SELECT * FROM {relation} WHERE ts_code='000300.SH' ORDER BY trade_date")
    calib = expected["benchmark"]["calibration_000300"]
    assert [r["trade_date"].strftime("%Y%m%d") for r in rows] == calib["overlap_dates"]
    assert [r["close"] for r in rows] == [1000, 1100, 1100, 1100]
    assert [r["pre_close"] for r in rows] == [1000, 1000, 1100, 1100]
    returns = [r["close"] / r["pre_close"] - 1 for r in rows]
    assert returns == pytest.approx(calib["official_returns"])


def test_self_built_benchmark_at_index16_matches_handwritten_values(built_store, expected):
    store, _ = built_store
    case = expected["benchmark"]["self_built_at_index16"]
    for method, key in (("equal_weight", "equal_weight_ret"), ("circ_mv_weight", "circ_mv_weight_ret")):
        row = _benchmark_row(store, case["trade_date"], method)
        assert row["member_date"].strftime("%Y%m%d") == case["member_date"]
        assert row["member_count"] == case["member_count"]
        assert row["return_count"] == case["return_count"]
        assert row["ret"] == pytest.approx(case[key], abs=1e-12), (method, row)
    # Independent closed-form re-derivation, not just the JSON constant.
    assert _benchmark_row(store, case["trade_date"], "equal_weight")["ret"] == pytest.approx(1 / 30)
    assert _benchmark_row(store, case["trade_date"], "circ_mv_weight")["ret"] == pytest.approx(
        0.1 * 665000 / (1000000 + 800000 + 665000))


def test_self_built_benchmark_flat_day_and_suspension_null_handling(built_store, expected):
    store, _ = built_store
    # index17: SA003's ret_1d is still NULL (prev quote index15 != prev trading
    # day index16), so return_count stays 3 and both benchmark returns are 0.
    for method in ("equal_weight", "circ_mv_weight"):
        row = _benchmark_row(store, "20200325", method)
        assert row["member_count"] == 4
        assert row["return_count"] == 3
        assert row["ret"] == pytest.approx(0.0)
        assert row["member_date"].strftime("%Y%m%d") == "20200324"


def test_benchmark_key_uniqueness_and_research_universe_coverage(built_store):
    store, _ = built_store
    relation = store.output_relation("l3", "daily_benchmark_return")
    duplicates = store.rows(f"""SELECT trade_date, benchmark_id, method, count(*) AS n
        FROM {relation} GROUP BY 1,2,3 HAVING count(*)>1 LIMIT 5""")
    assert duplicates == []
    # all_a_ex_st has real members here (no ipo rule, no ST, listed), but in
    # this fixture every stock's list_date predates the calendar, so the
    # ipo120 default universe resolves to LISTING_AGE_UNKNOWN (is_in_universe
    # NULL, never TRUE): no rows for it may appear.
    counts = store.rows(f"SELECT benchmark_id, count(*) AS n FROM {relation} GROUP BY 1")
    assert {r["benchmark_id"] for r in counts} == {"index_member_hs300", "all_a_ex_st"}


def test_calibration_report_matches_closed_form(built_store, expected):
    store, destination = built_store
    report = build_benchmark_calibration(store, config_root=destination)
    case = expected["benchmark"]["calibration_000300"]
    assert report["basis"] == "self_built_approximation_not_official_index"
    equal = report["methods"]["equal_weight"]
    assert equal["n"] == case["n"] == 4
    assert equal["pearson"] == pytest.approx(1.0, abs=1e-12)
    assert equal["mean_diff"] == pytest.approx(-1 / 60, abs=1e-12)
    assert equal["tracking_error_daily"] == pytest.approx(math.sqrt(1 / 1200), abs=1e-12)
    assert equal["tracking_error_annualized"] == pytest.approx(math.sqrt(1 / 1200) * math.sqrt(252), abs=1e-12)
    assert equal["sufficient_overlap"] is True
    assert equal["overlap_start"] == "2020-03-23" and equal["overlap_end"] == "2020-03-26"
    weighted = report["methods"]["circ_mv_weight"]
    assert weighted["n"] == 4
    assert weighted["pearson"] == pytest.approx(1.0, abs=1e-12)
    markdown = store.paths.output("l3", "daily_benchmark_return", "calibration.md").read_text(encoding="utf-8")
    assert "自建近似" in markdown
    assert "不是官方指数" in markdown


def test_calibration_metrics_hand_computed_example():
    # Fully hand-computed independent example (not the fixture): with ddof=0,
    # cov=0.000175, var_official=0.000125, var_built=0.000275, and the diff
    # series [0,-0.01,0,0.01] has mean 0 and variance 0.00005.
    pairs = [(0.01, 0.01), (0.02, 0.01), (0.03, 0.03), (0.04, 0.05)]
    metrics = calibration_metrics(pairs)
    assert metrics["n"] == 4
    assert metrics["pearson"] == pytest.approx(0.000175 / math.sqrt(0.000125 * 0.000275), abs=1e-12)
    assert metrics["mean_diff"] == pytest.approx(0.0, abs=1e-15)
    assert metrics["tracking_error_daily"] == pytest.approx(math.sqrt(0.00005), abs=1e-12)


def test_calibration_metrics_degenerate_inputs():
    assert calibration_metrics([])["n"] == 0
    assert calibration_metrics([])["pearson"] is None
    # Constant built series: Pearson undefined, tracking error still defined.
    # diffs [-0.01,-0.02], mean -0.015, ddof=0 variance 0.000025 -> te = 0.005.
    metrics = calibration_metrics([(0.01, 0.0), (0.02, 0.0)])
    assert metrics["pearson"] is None
    assert metrics["tracking_error_daily"] == pytest.approx(0.005, abs=1e-12)
    single = calibration_metrics([(0.02, 0.01)])
    assert single["pearson"] is None
    assert single["mean_diff"] == pytest.approx(-0.01)
    assert single["tracking_error_daily"] == 0.0


def test_benchmark_modules_pass_raw_access_guard():
    assert check_no_raw_access() == []
