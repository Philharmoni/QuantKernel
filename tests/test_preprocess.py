"""Step 6 preprocessing: exact hand-computed formula checks on the pure ops,
plus an end-to-end factor_processed build on the golden_stage2 fixture with
independently re-derived cross-sections and OLS orthogonality properties.
"""
import json
import math
import shutil
import statistics

import numpy as np
import pytest
import yaml

from core.calendar import build_calendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store, qs
from core.factor.baseline import build_factor_values
from core.factor.entry import check_factor_label_isolation, check_no_raw_access
from core.financial.available import build_financial_available
from core.financial.quarters import build_quarterly_and_ttm
from core.industry.pit import build_daily_industry
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.preprocess.ops import neutralize_within, process_cross_section, winsorize_mad, zscore
from core.preprocess.panel import build_factor_processed
from core.reference.membership import build_index_member_available
from core.universe.research import build_research_universe


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


def test_winsorize_and_zscore_match_handwritten_values(expected):
    case = expected["evaluation_formula_fixture"]["winsorize_standardize_on_8_stock_panel"]
    codes = list(case["raw"])
    raw = np.array([case["raw"][c] for c in codes], dtype=np.float64)
    clipped, info = winsorize_mad(raw, case["winsorize_k"])
    assert info["median"] == case["median"]
    assert info["mad"] == case["mad"]
    assert (info["lower"], info["upper"]) == tuple(case["winsorize_bound"])
    assert info["clipped"] == 1 and info["zero_mad"] is False
    for code, value in zip(codes, clipped):
        assert value == case["winsorized"][code]
    z, zinfo = zscore(clipped)
    assert zinfo["mean"] == case["standardize_mean"]
    assert zinfo["std"] == case["standardize_std"]
    for code, value in zip(codes, z):
        assert value == pytest.approx(case["zscore"][code], abs=1e-15)


def test_winsorize_zero_mad_skips_clipping():
    values = np.array([5.0, 5.0, 5.0, 9.0])
    clipped, info = winsorize_mad(values, 5)
    assert info["zero_mad"] is True and info["clipped"] == 0
    assert np.array_equal(clipped, values)


def test_neutralize_within_hand_computed_two_group_case():
    # Group A: z=[0,2], x=[0,2]; group B: z=[1,3], x=[0,1].
    # numerator = 2 + 1 = 3, denominator = 2 + 0.5 = 2.5 -> beta = 1.2.
    # residuals: A [0.2,-0.2], B [-0.4,0.4]; group sums 0 and sum(residual*x) 0.
    z = np.array([0.0, 2.0, 1.0, 3.0])
    x = np.array([0.0, 2.0, 0.0, 1.0])
    groups = np.array(["A", "A", "B", "B"], dtype=object)
    residual, info = neutralize_within(z, x, groups)
    assert info["beta"] == pytest.approx(1.2)
    assert info["groups"] == 2 and info["df"] == 1
    assert residual == pytest.approx([0.2, -0.2, -0.4, 0.4])
    assert float(np.sum(residual)) == pytest.approx(0.0, abs=1e-15)
    assert float(np.sum(residual * x)) == pytest.approx(0.0, abs=1e-15)
    assert info["max_abs_group_sum_residual"] == pytest.approx(0.0, abs=1e-15)
    assert info["abs_sum_residual_x"] == pytest.approx(0.0, abs=1e-15)


def test_process_cross_section_skip_paths_and_unknown_industry():
    # Too few valid values: no output, reason recorded, nothing forward-filled.
    result = process_cross_section([1.0, np.nan, 2.0], ["A", "A", "A"], [1.0, 1.0, 1.0],
                                   winsorize_k=5, unknown_label="__UNKNOWN__",
                                   min_cross_section=5, min_neutralization_df=1,
                                   neutral_process_id="neutral")
    assert result.values == {}
    assert result.diagnostics["skip_reason"] == "insufficient_cross_section"
    # Constant cross-section: winsorize/standardize cannot produce a z-score.
    result = process_cross_section([7.0] * 6, ["A"] * 6, [1.0] * 6,
                                   winsorize_k=5, unknown_label="__UNKNOWN__",
                                   min_cross_section=5, min_neutralization_df=1,
                                   neutral_process_id="neutral")
    assert result.values == {}
    assert result.diagnostics["skip_reason"] == "zero_variance"
    # Unknown industries form their own dummy and are counted; missing
    # log_circ_mv rows are excluded only from the neutral version.
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 100.0]
    industries = ["A", "A", "B", None, "", "B"]
    # mv deliberately not a linear function of values, otherwise the residual
    # would be degenerate (covered by the collinearity test).
    mv = [5.0, 1.0, 4.0, 2.0, 3.0, np.nan]
    result = process_cross_section(values, industries, mv, winsorize_k=5,
                                   unknown_label="__UNKNOWN__", min_cross_section=5,
                                   min_neutralization_df=1, neutral_process_id="neutral")
    assert result.diagnostics["unknown_industry_count"] == 2
    assert result.diagnostics["unknown_industry_share"] == pytest.approx(2 / 6)
    assert result.diagnostics["neutralize_missing_regressor"] == 1
    assert set(result.values) == {"ws_z", "neutral"}
    indices, neutral = result.values["neutral"]
    assert len(indices) == len(neutral) == 5  # the NaN-regressor stock is absent


def _fixture_config(tmp_path, min_cross_section=5):
    destination = tmp_path / "configs"
    destination.mkdir(exist_ok=True)
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml", "factor_research.yaml",
                     "factor_registry.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / filename, destination / filename)
    path = destination / "data_middle_layer.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules["trade_status"].update(
        limit_events_complete=True, suspension_events_complete=True,
        suspension_complete_code_suffixes=["SH", "SZ", "BJ"],
        coverage_start="20200302", coverage_end="20200605",
        limit_coverage_start="20200302", limit_coverage_end="20200605")
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    research_path = destination / "factor_research.yaml"
    research = yaml.safe_load(research_path.read_text(encoding="utf-8"))
    # The ipo120 default universe is empty on this fixture (all list_dates
    # predate the calendar), so process all_a_ex_st; 8-stock cross-sections need
    # smaller sample thresholds than production's 30/10.
    research["preprocessing"].update(universes=["all_a_ex_st"],
                                     min_cross_section=min_cross_section,
                                     min_neutralization_df=3)
    research_path.write_text(yaml.safe_dump(research, allow_unicode=True), encoding="utf-8")
    return destination


def _build_fixture_store(tmp_path, min_cross_section=5):
    destination = _fixture_config(tmp_path, min_cross_section)
    store = Store(Paths(PROJECT_ROOT / "tests" / "fixtures" / "golden_stage2",
                        tmp_path / "middle"), config_root=destination)
    build_calendar(store)
    build_adjusted_price(store)
    build_stock_state(store)
    build_trade_status(store)
    build_research_universe(store)
    build_financial_available(store)
    build_quarterly_and_ttm(store)
    build_index_member_available(store)
    build_daily_industry(store)
    build_factor_values(store)
    build_factor_processed(store, config_root=destination)
    return store, destination


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    store, destination = _build_fixture_store(tmp_path_factory.mktemp("preprocess"))
    yield store, destination
    store.close()


def _processed(store, trade_date, factor_id, process_id, universe="all_a_ex_st"):
    relation = store.output_relation("l3", "factor_processed")
    return store.rows(f"SELECT ts_code, value FROM {relation} WHERE trade_date=DATE {qs(trade_date)} "
                      f"AND factor_id={qs(factor_id)} AND universe_id={qs(universe)} "
                      f"AND process_id={qs(process_id)} ORDER BY ts_code")


def test_ws_z_matches_independent_stdlib_recomputation(built):
    store, _ = built
    trade_date = "2020-03-23"
    raw = {r["ts_code"]: r["value"] for r in store.rows(
        f"SELECT ts_code, value FROM {store.output_relation('l3', 'factor_value')} "
        f"WHERE factor_id='log_total_mv' AND trade_date=DATE {qs(trade_date)}")}
    assert len(raw) == 8
    values = list(raw.values())
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values])
    lower, upper = median - 5 * mad, median + 5 * mad
    clipped = [min(max(v, lower), upper) for v in values]
    mean = sum(clipped) / len(clipped)
    std = math.sqrt(sum((v - mean) ** 2 for v in clipped) / len(clipped))
    expected_z = {code: (clipped[i] - mean) / std for i, code in enumerate(raw)}
    actual = {r["ts_code"]: r["value"] for r in _processed(store, trade_date, "log_total_mv", "ws_z")}
    assert actual.keys() == expected_z.keys()
    for code in actual:
        assert actual[code] == pytest.approx(expected_z[code], abs=1e-12), code
    assert sum(actual.values()) == pytest.approx(0.0, abs=1e-12)
    assert math.sqrt(sum(v * v for v in actual.values()) / len(actual)) == pytest.approx(1.0, abs=1e-12)


def test_neutral_version_is_orthogonal_to_industry_and_market_cap(built):
    store, _ = built
    # rev_20 at index35 has genuine cross-sectional variation (SA004=0.1,
    # SA005=115/105-1, others 0) and is NOT collinear with log_circ_mv, unlike
    # log_total_mv (covered by the degenerate-case test below).
    trade_date = "2020-04-21"
    actual = {r["ts_code"]: r["value"] for r in _processed(
        store, trade_date, "rev_20", "ws_z_industry_mv_neutral_z")}
    assert actual
    relation = store.output_relation("l3", "factor_value")
    mv = {r["ts_code"]: r["value"] for r in store.rows(
        f"SELECT ts_code, value FROM {relation} WHERE factor_id='log_circ_mv' "
        f"AND trade_date=DATE {qs(trade_date)}")}
    assert sum(actual.values()) == pytest.approx(0.0, abs=1e-9)
    assert sum(actual[c] * mv[c] for c in actual) == pytest.approx(0.0, abs=1e-9)
    industry = {r["ts_code"]: r["industry"] for r in store.rows(
        f"SELECT ts_code, CASE WHEN industry_pit_status IN ('pit_history','snapshot_after_in_date') "
        f"THEN l1_code END AS industry FROM {store.output_relation('l3', 'daily_industry')} "
        f"WHERE trade_date=DATE {qs(trade_date)}")}
    groups = {}
    for code, value in actual.items():
        groups.setdefault(industry[code], []).append(value)
    for group, members in groups.items():
        assert sum(members) == pytest.approx(0.0, abs=1e-9), (group, members)


def test_neutral_version_skipped_when_factor_is_collinear_with_regressor(built):
    store, _ = built
    # In this fixture total_share == float_share for every stock, so
    # log_total_mv == log_circ_mv exactly: the neutralization fits perfectly,
    # the residual is numerically ~0, and re-standardizing would only amplify
    # rounding noise. The neutral version must be absent, with the reason
    # recorded -- not a garbage cross-section.
    assert _processed(store, "2020-03-23", "log_total_mv", "ws_z_industry_mv_neutral_z") == []
    diag = "read_parquet({})".format(qs(store.paths.output(
        "l3", "factor_processed", "processing_diagnostics.parquet").as_posix()))
    row = store.rows(f"SELECT neutralize_skip_reason FROM {diag} "
                     f"WHERE factor_id='log_total_mv' AND trade_date=DATE '2020-03-23'")
    assert row[0]["neutralize_skip_reason"] == "degenerate_residual_variance"


def test_processing_diagnostics_unknown_share_and_reasons(built):
    store, _ = built
    relation = "read_parquet({})".format(qs(store.paths.output(
        "l3", "factor_processed", "processing_diagnostics.parquet").as_posix()))
    at_t = store.rows(f"SELECT * FROM {relation} WHERE factor_id='log_total_mv' "
                      f"AND trade_date=DATE '2020-03-23'")
    assert len(at_t) == 1
    assert at_t[0]["unknown_industry_count"] == 1
    assert at_t[0]["unknown_industry_share"] == pytest.approx(1 / 8)
    later = store.rows(f"SELECT * FROM {relation} WHERE factor_id='log_total_mv' "
                       f"AND trade_date=DATE '2020-04-14'")
    assert later[0]["unknown_industry_count"] == 0  # SA007 is a known member now
    residuals = store.rows(f"SELECT max(max_abs_group_sum_residual) AS g, max(abs_sum_residual_x) AS x "
                           f"FROM {relation}")
    assert residuals[0]["g"] < 1e-8 and residuals[0]["x"] < 1e-8
    assert store.paths.output("l3", "factor_processed", "processing_report.md").is_file()


def test_insufficient_cross_section_produces_no_rows(tmp_path):
    store, destination = _build_fixture_store(tmp_path, min_cross_section=9)
    with store:
        relation = store.output_relation("l3", "factor_processed")
        assert store.rows(f"SELECT count(*) AS n FROM {relation}")[0]["n"] == 0
        diag = "read_parquet({})".format(qs(store.paths.output(
            "l3", "factor_processed", "processing_diagnostics.parquet").as_posix()))
        reasons = store.rows(f"SELECT DISTINCT skip_reason FROM {diag}")
        assert {r["skip_reason"] for r in reasons} == {"insufficient_cross_section"}


def test_preprocess_module_isolation_guards():
    assert check_no_raw_access() == []
    assert check_factor_label_isolation() == []
