"""Step 7 evaluation formulas, panel products, repeatability and OOS locking."""
from datetime import date, timedelta
import json
import math
import shutil

import numpy as np
import pyarrow as pa
import pytest
import yaml

from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store
from core.evaluation.builder import _add_quantile_summary, evaluate_factors
from core.evaluation.ops import (average_rank, newey_west_t, one_way_turnover,
                                 performance_metrics, quantile_group_ids,
                                 rank_ic, summarize_ic)
from core.evaluation.trial import append_trial_log, read_trial_log


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


def test_ic_and_rank_ic_match_handwritten_tie_fixture(expected):
    case = expected["evaluation_formula_fixture"]["ic_rank_ic_with_ties"]
    pairs = [value for value in case["raw_pairs_8_stocks"].values() if value["ret5"] is not None]
    factors = [value["factor"] for value in pairs]
    returns = [value["ret5"] for value in pairs]
    assert len(pairs) == case["valid_pairs_n"]
    from core.evaluation.ops import pearson
    assert pearson(factors, returns) == pytest.approx(case["pearson_ic"], abs=1e-15)
    assert rank_ic(factors, returns) == case["spearman_rank_ic"]
    factor_ranks = average_rank(factors)
    return_ranks = average_rank(returns)
    expected_factor_ranks = case["average_rank_of_factor_among_valid_7"]
    expected_return_ranks = case["average_rank_of_ret5_among_valid_7"]
    valid_codes = [code for code, value in case["raw_pairs_8_stocks"].items() if value["ret5"] is not None]
    for code, factor_rank, return_rank in zip(valid_codes, factor_ranks, return_ranks):
        assert factor_rank == expected_factor_ranks[code]
        assert return_rank == expected_return_ranks[code]


def test_newey_west_and_ic_summary_match_closed_forms(expected):
    case = expected["evaluation_formula_fixture"]["newey_west_t_closed_form"]
    assert newey_west_t(case["series"], case["horizon"]) == pytest.approx(case["expected_t"])
    assert newey_west_t([1, 2, 3, 4], 1) == pytest.approx(2.5 / (math.sqrt(5 / 3) / 2))
    assert newey_west_t([1, 1, 1], 5) is None
    result = summarize_ic([0.1, -0.2, 0.3], [0.2, 0.0, -0.2], 5, 252)
    assert result["ic"]["mean"] == pytest.approx(1 / 15)
    assert result["ic"]["std"] == pytest.approx(math.sqrt(19 / 450))
    assert result["ic"]["win_rate"] == pytest.approx(2 / 3)
    assert result["rank_ic"]["mean"] == 0.0
    assert result["rank_icir"] == 0.0 and result["rank_ic_t_stat"] == 0.0


def test_group_performance_matches_handwritten_fixture(expected):
    case = expected["evaluation_formula_fixture"]["group_performance_standalone_series"]
    dates = [date(2020, 1, 2) + timedelta(days=20 * i) for i in range(4)]
    for prefix in ("group1", "long_short"):
        actual = performance_metrics(case[f"{prefix}_returns"], dates, case["periods_per_year"])
        for metric, value in case[prefix].items():
            if metric in ("wealth_path", "drawdown_path"):
                continue
            assert actual[metric] == pytest.approx(value, abs=1e-14), (prefix, metric)
        assert actual["n_periods"] == 4
    assert actual["max_drawdown_start"] == "2020-01-02"
    assert actual["max_drawdown_end"] == "2020-03-02"


def test_quantile_direction_ties_and_turnover():
    # Expected direction +1: largest values form group 1. Ties receive average
    # ranks and therefore cannot cross a quantile edge inconsistently.
    groups = quantile_group_ids([1, 2, 2, 4], 1, 2)
    assert groups.tolist() == [2, 2, 2, 1]
    assert quantile_group_ids([1, 2, 2, 4], -1, 2).tolist() == [1, 1, 1, 2]
    assert one_way_turnover([], ["A"]) is None
    assert one_way_turnover(["A", "B"], ["B", "C"]) == pytest.approx(0.5)


def test_breakeven_cost_and_monotonicity_closed_form():
    row = {"label_id": "ret_o2o_5"}
    offset_rows = [{"rebalance_offset": 0, "dates": [date(2020, 1, 2), date(2020, 1, 28)],
                    "long_returns": [0.10, 0.20], "short_returns": [0.02, 0.04],
                    "long_turnovers": [None, 0.5], "short_turnovers": [None, 0.25],
                    "group_counts": [4, 4]}]
    monotonicity = {0: [(1, 0.15), (2, 0.10), (3, 0.05), (4, 0.03)]}
    _add_quantile_summary(row, offset_rows, monotonicity, 252)
    assert row["group1_mean_period_return"] == pytest.approx(0.15)
    assert row["group1_mean_one_way_turnover"] == pytest.approx(0.5)
    assert row["group1_breakeven_cost"] == pytest.approx(0.3)
    assert row["long_short_mean_period_return"] == pytest.approx(0.12)
    assert row["long_short_mean_one_way_turnover"] == pytest.approx(0.75)
    assert row["long_short_breakeven_cost"] == pytest.approx(0.16)
    assert row["monotonicity"] == pytest.approx(1.0)


def _configs(tmp_path):
    destination = tmp_path / "configs"
    destination.mkdir()
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml",
                     "factor_research.yaml", "factor_registry.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / filename, destination / filename)
    path = destination / "factor_research.yaml"
    research = yaml.safe_load(path.read_text(encoding="utf-8"))
    research["sample"].update(evaluation_start="20200302", in_sample_end="20200312",
                              quote_coverage_end="20200317", min_cross_section_count=6,
                              quantiles=4, min_quantiles=2)
    research["preprocessing"].update(universes=["all_a_ex_st"], processes=[
        {"process_id": "ws_z", "neutralize": False}])
    research["evaluation"].update(segment_horizons=[5], quantile_min_group_size=1)
    path.write_text(yaml.safe_dump(research, allow_unicode=True), encoding="utf-8")
    return destination


def _publish(store, layer, name, rows, key):
    table = pa.Table.from_pylist(rows)
    store.db.register(f"{name}_input", table)
    result = store.publish(layer, name, f"SELECT * FROM {name}_input", key)
    store.db.unregister(f"{name}_input")
    return result


def _build_panel_store(tmp_path):
    config_root = _configs(tmp_path)
    store = Store(Paths(PROJECT_ROOT / "tests" / "fixtures" / "golden_stage2",
                        tmp_path / "middle"), config_root=config_root)
    dates = [date(2020, 3, 2) + timedelta(days=i) for i in range(12)]
    codes = [f"SA{i:03d}.SZ" for i in range(1, 9)]
    _publish(store, "l1", "daily_calendar",
             [{"trade_date": day, "trade_index": i + 1, "exchange": "SSE",
               "calendar_start": dates[0], "calendar_end": dates[-1],
               "prev_trade_date": dates[i - 1] if i else None,
               "next_trade_date": dates[i + 1] if i + 1 < len(dates) else None}
              for i, day in enumerate(dates)], ["trade_date"])
    processed, market_values, labels = [], [], []
    for day_index, day in enumerate(dates):
        for stock_index, code in enumerate(codes):
            processed.append({"trade_date": day, "ts_code": code, "factor_id": "rev_20",
                              "universe_id": "all_a_ex_st", "process_id": "ws_z",
                              "value": float((stock_index + day_index) % 8 + 1)})
            market_values.append({"trade_date": day, "ts_code": code, "factor_id": "log_circ_mv",
                                  "value": float(20 + stock_index)})
            for horizon in (1, 5):
                labels.append({"trade_date": day, "ts_code": code,
                               "label_id": f"ret_o2o_{horizon}",
                               "ret": ((stock_index * 2 + day_index) % 7 - 3) / 100,
                               "entry_open_at_up_limit": bool(day_index == 2 and stock_index == 0)})
    _publish(store, "l3", "factor_processed", processed,
             ["trade_date", "ts_code", "factor_id", "universe_id", "process_id"])
    _publish(store, "l3", "factor_value", market_values, ["trade_date", "ts_code", "factor_id"])
    _publish(store, "l3", "forward_return", labels, ["trade_date", "ts_code", "label_id"])
    members = [{"trade_date": day, "index_code": "000300.SH", "con_code": code}
               for day in dates for code in codes[:4]]
    _publish(store, "l3", "daily_index_member", members, ["trade_date", "index_code", "con_code"])
    return store, config_root


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    store, config_root = _build_panel_store(tmp_path_factory.mktemp("evaluation"))
    result = evaluate_factors(store, factor_ids=["rev_20"], process_ids=["ws_z"],
                              horizons=[1, 5], config_root=config_root)
    yield store, config_root, result
    store.close()


def test_evaluation_panel_products_and_segments(evaluated):
    store, _, result = evaluated
    ic = store.output_relation("l4", "factor_ic_daily")
    quantiles = store.output_relation("l4", "factor_quantile_return")
    summary = store.output_relation("l4", "factor_summary")
    assert result["factor_ic_daily"]["rows"] > 0
    assert store.rows(f"SELECT min(trade_date) AS lo, max(trade_date) AS hi FROM {ic}")[0] == {
        "lo": date(2020, 3, 2), "hi": date(2020, 3, 12)}
    segments = store.rows(f"SELECT label_id, segment_id, count(*) AS n FROM {ic} "
                          "GROUP BY label_id, segment_id ORDER BY label_id, segment_id")
    assert {row["segment_id"] for row in segments if row["label_id"] == "ret_o2o_1"} == {"full"}
    assert {row["segment_id"] for row in segments if row["label_id"] == "ret_o2o_5"} >= {
        "full", "cap_1", "cap_2", "cap_3", "index_member_hs300"}
    offsets = store.rows(f"SELECT DISTINCT rebalance_offset FROM {quantiles} "
                         "WHERE label_id='ret_o2o_5' AND segment_id='full' ORDER BY rebalance_offset")
    assert [row["rebalance_offset"] for row in offsets] == [0, 1, 2, 3, 4]
    first_turnover = store.rows(f"SELECT turnover FROM {quantiles} WHERE label_id='ret_o2o_5' "
                                "AND segment_id='full' AND rebalance_offset=0 AND group_id=1 "
                                "ORDER BY trade_date LIMIT 1")
    assert first_turnover[0]["turnover"] is None
    periods = store.rows(f"SELECT DISTINCT period_id FROM {summary} WHERE label_id='ret_o2o_1' "
                         "AND segment_id='full' ORDER BY period_id")
    assert [row["period_id"] for row in periods] == ["full", "in_sample", "year:2020"]
    assert read_trial_log(store)[-1]["oos_accessed"] is False


def test_summary_reconciles_to_daily_ic(evaluated):
    store, _, _ = evaluated
    ic = store.output_relation("l4", "factor_ic_daily")
    summary = store.output_relation("l4", "factor_summary")
    daily = store.rows(f"SELECT rank_ic FROM {ic} WHERE label_id='ret_o2o_5' "
                       "AND sample_mode='theoretical' AND segment_id='full' ORDER BY trade_date")
    row = store.rows(f"SELECT * FROM {summary} WHERE label_id='ret_o2o_5' "
                     "AND sample_mode='theoretical' AND segment_id='full' AND period_id='full'")[0]
    values = [item["rank_ic"] for item in daily if item["rank_ic"] is not None]
    assert row["rank_ic_n_days"] == len(values)
    assert row["rank_ic_mean"] == pytest.approx(float(np.mean(values)))
    assert row["rank_ic_std"] == pytest.approx(float(np.std(values)))
    assert row["rank_ic_t_stat"] == pytest.approx(newey_west_t(values, 5))
    assert row["rebalance_offsets"] == 5
    assert row["group1_n_periods"] > 0 and row["long_short_n_periods"] > 0
    assert row["group1_mean_one_way_turnover"] > 0
    assert row["long_short_mean_one_way_turnover"] > 0


def test_evaluation_is_reproducible_except_append_only_trial_ids(evaluated):
    store, config_root, first = evaluated
    second = evaluate_factors(store, factor_ids=["rev_20"], process_ids=["ws_z"],
                              horizons=[1, 5], config_root=config_root)
    for name in ("factor_ic_daily", "factor_quantile_return", "factor_summary"):
        assert second[name]["sha256"] == first[name]["sha256"], name
    assert len(read_trial_log(store)) == 2


def test_out_of_sample_second_access_is_rejected_before_build(evaluated):
    store, config_root, _ = evaluated
    append_trial_log(store, [{"factor_id": "rev_20", "factor_version": 1,
                              "oos_accessed": True, "config_root": config_root}])
    with pytest.raises(PermissionError, match="Out-of-sample access denied"):
        evaluate_factors(store, start="2020-03-13", end="2020-03-17", factor_ids=["rev_20"],
                         process_ids=["ws_z"], horizons=[1], include_out_of_sample=True,
                         config_root=config_root)
