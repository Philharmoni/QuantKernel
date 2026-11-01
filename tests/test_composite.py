"""Step 8 correlation, transparent composites, cutoff gates and Step 5-7 reuse."""
from datetime import date, timedelta
import json
import shutil

import pyarrow as pa
import pytest
import yaml

from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store
from core.evaluation.builder import evaluate_factors
from core.evaluation.composite import build_composite_factors, signed_icir_weights
from core.evaluation.correlation import build_factor_correlation, complete_linkage_clusters
from core.factor.truncate import check_truncation_invariance
from core.preprocess.panel import build_factor_processed


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


def _history(signal_offsets, exit_offsets, rank_ics):
    base = date(2020, 1, 2)
    return [{"signal_date": base + timedelta(days=s), "max_exit_date": base + timedelta(days=e),
             "rank_ic": value} for s, e, value in zip(signal_offsets, exit_offsets, rank_ics)]


def test_signed_icir_weights_closed_form_and_strict_exit_boundary(expected):
    case = expected["composition_formula_fixture"]["rolling_icir_signed_weights"]
    histories = {name: _history(case[f"component_{name}"]["signal_offsets"],
                                case[f"component_{name}"]["exit_offsets"],
                                case[f"component_{name}"]["rank_ics"]) for name in ("A", "B")}
    result = signed_icir_weights(histories, case["trade_date"], case["lookback_observations"],
                                 case["min_periods"], case["in_sample_end"])
    for name in ("A", "B"):
        wanted = case[f"expected_{name}"]
        assert result[name]["ic_mean"] == wanted["mean"]
        assert result[name]["ic_std"] == wanted["std"]
        assert result[name]["icir"] == wanted["icir"]
        assert result[name]["weight"] == wanted["weight"]
    # At 2020-01-05, the second observation exits exactly at T and is not visible.
    boundary = signed_icir_weights(histories, "2020-01-05", 3, 2, "2020-01-31")
    assert boundary["A"]["n_history"] == 1 and boundary["A"]["weight"] == 0.0
    # The in-sample cutoff is independent of T: later-realized labels stay locked.
    locked = signed_icir_weights(histories, "2020-02-01", 3, 2, "2020-01-02")
    assert locked["A"]["n_history"] == 0


def test_complete_linkage_redundancy_clusters():
    pairs = {("A", "B"): {"n_days": 30, "mean_correlation": 0.9},
             ("A", "C"): {"n_days": 30, "mean_correlation": 0.1},
             ("B", "C"): {"n_days": 30, "mean_correlation": 0.8}}
    assert complete_linkage_clusters(["C", "A", "B"], pairs, 0.7, 20) == [["A", "B"], ["C"]]
    pairs[("A", "C")]["mean_correlation"] = -0.85
    assert complete_linkage_clusters(["A", "B", "C"], pairs, 0.7, 20) == [["A", "B", "C"]]


def _configs(tmp_path):
    destination = tmp_path / "configs"
    destination.mkdir()
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml",
                     "factor_research.yaml", "factor_registry.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / filename, destination / filename)
    path = destination / "factor_research.yaml"
    research = yaml.safe_load(path.read_text(encoding="utf-8"))
    research["sample"].update(evaluation_start="20200302", in_sample_end="20200311",
                              quote_coverage_end="20200313", min_cross_section_count=4,
                              quantiles=4, min_quantiles=2)
    research["preprocessing"].update(universes=["all_a_ex_st"], min_cross_section=4,
                                     min_neutralization_df=2,
                                     processes=[{"process_id": "ws_z", "neutralize": False}])
    research["composition"].update(universe_id="all_a_ex_st", process_id="ws_z",
                                   components=["rev_20", "vol_20"], min_component_count=1)
    research["composition"]["correlation"].update(min_pair_days=2, redundancy_threshold=0.7)
    research["composition"]["composites"][1].update(label_id="ret_o2o_1", lookback_observations=3,
                                                     min_periods=2)
    path.write_text(yaml.safe_dump(research, allow_unicode=True), encoding="utf-8")
    return destination


def _publish(store, layer, name, rows, key):
    frame = pa.Table.from_pylist(rows)
    store.db.register(f"{name}_input", frame)
    result = store.publish(layer, name, f"SELECT * FROM {name}_input", key)
    store.db.unregister(f"{name}_input")
    return result


def _build_store(tmp_path):
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
    processed, values, labels, universe, industry = [], [], [], [], []
    for day_index, day in enumerate(dates):
        for stock_index, code in enumerate(codes):
            rev = float(stock_index + 1 + (day_index % 3) * stock_index)
            vol = float(8 - stock_index + (day_index % 2) * 0.25)
            # One deliberately missing component: equal weighting must renormalize
            # over the remaining available weight rather than fill a value.
            if not (day_index == 0 and stock_index == 0):
                processed.append({"trade_date": day, "ts_code": code, "factor_id": "rev_20",
                                  "universe_id": "all_a_ex_st", "process_id": "ws_z", "value": rev})
                values.append({"trade_date": day, "ts_code": code, "factor_id": "rev_20", "value": rev})
            processed.append({"trade_date": day, "ts_code": code, "factor_id": "vol_20",
                              "universe_id": "all_a_ex_st", "process_id": "ws_z", "value": vol})
            values.extend([
                {"trade_date": day, "ts_code": code, "factor_id": "vol_20", "value": vol},
                {"trade_date": day, "ts_code": code, "factor_id": "log_circ_mv",
                 "value": float(20 + stock_index)}])
            return_rank = (stock_index * (day_index % 3 + 1) + day_index) % 8
            ret = (3 - return_rank) / 100
            labels.append({"trade_date": day, "ts_code": code, "label_id": "ret_o2o_1",
                           "ret": ret if day_index + 1 < len(dates) else None,
                           "exit_date": dates[day_index + 1] if day_index + 1 < len(dates) else None,
                           "entry_open_at_up_limit": False})
            universe.append({"trade_date": day, "ts_code": code, "universe_id": "all_a_ex_st",
                             "is_in_universe": True})
            industry.append({"trade_date": day, "ts_code": code, "l1_code": "TEST",
                             "industry_pit_status": "snapshot_after_in_date"})
    _publish(store, "l3", "factor_processed", processed,
             ["trade_date", "ts_code", "factor_id", "universe_id", "process_id"])
    _publish(store, "l3", "factor_value", values, ["trade_date", "ts_code", "factor_id"])
    _publish(store, "l3", "forward_return", labels, ["trade_date", "ts_code", "label_id"])
    _publish(store, "l2", "daily_research_universe", universe,
             ["trade_date", "ts_code", "universe_id"])
    _publish(store, "l3", "daily_industry", industry, ["trade_date", "ts_code"])
    members = [{"trade_date": day, "index_code": "000300.SH", "con_code": code}
               for day in dates for code in codes[:4]]
    _publish(store, "l3", "daily_index_member", members, ["trade_date", "index_code", "con_code"])
    return store, config_root


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    store, config_root = _build_store(tmp_path_factory.mktemp("composite"))
    correlation = build_factor_correlation(store, config_root=config_root)
    result = build_composite_factors(store, config_root=config_root)
    yield store, config_root, correlation, result
    store.close()


def test_correlation_product_and_clusters(built):
    store, _, correlation, _ = built
    relation = store.output_relation("l4", "factor_correlation")
    row = store.rows(f"SELECT * FROM {relation} WHERE factor_a='rev_20' AND factor_b='vol_20'")[0]
    assert row["n_days"] == 10 and abs(row["mean_correlation"]) <= 1
    assert correlation["manifest"]["rows"] == 1
    assert len(correlation["clusters"]) >= 1


def test_equal_composite_hand_value_and_missing_renormalization(built):
    store, _, _, _ = built
    relation = store.output_relation("l3", "factor_composite_value")
    row = store.rows(f"SELECT value FROM {relation} WHERE factor_id='composite_equal' "
                     "AND trade_date=DATE '2020-03-02' AND ts_code='SA001.SZ'")[0]
    # rev_20 is absent; the remaining vol_20=8.0 has expected_direction=-1.
    assert row["value"] == pytest.approx(-8.0)
    row = store.rows(f"SELECT value FROM {relation} WHERE factor_id='composite_equal' "
                     "AND trade_date=DATE '2020-03-02' AND ts_code='SA002.SZ'")[0]
    assert row["value"] == pytest.approx(-(2.0 + 7.0) / 2)


def test_weight_visibility_normalization_and_in_sample_lock(built):
    store, _, _, _ = built
    relation = store.output_relation("l4", "factor_composite_weight")
    rows = store.rows(f"SELECT * FROM {relation} WHERE composite_id='composite_icir_60' "
                      "AND trade_date>=DATE '2020-03-05' ORDER BY trade_date,component_id")
    assert rows
    for row in rows:
        assert row["last_exit_date"] is None or row["last_exit_date"] < row["trade_date"]
        assert row["last_exit_date"] is None or row["last_exit_date"] <= date(2020, 3, 11)
    by_date = {}
    for row in rows:
        by_date.setdefault(row["trade_date"], []).append(abs(row["weight"]))
    assert any(abs(sum(weights) - 1.0) < 1e-12 for weights in by_date.values())
    realized = store.output_relation("l4", "factor_ic_realized")
    assert store.rows(f"SELECT count(*) AS n FROM {realized} "
                      "WHERE max_exit_date>DATE '2020-03-11'")[0]["n"] == 0


def test_composite_truncation_invariance_on_every_date(built):
    store, config_root, _, _ = built
    dates = [row["trade_date"] for row in store.rows(
        f"SELECT trade_date FROM {store.output_relation('l1', 'daily_calendar')} ORDER BY trade_date")]
    report = check_truncation_invariance(
        store, dates, factor_ids=["composite_equal", "composite_icir_60"], config_root=config_root)
    assert report["compared_values"] > 0
    assert report["mismatches"] == [], report["mismatches"][:5]


def test_composite_rebuild_is_bit_reproducible(built):
    store, config_root, _, first = built
    second = build_composite_factors(store, config_root=config_root)
    for name in ("factor_ic_realized", "factor_composite_weight", "factor_composite_value", "factor_value"):
        assert second[name]["sha256"] == first[name]["sha256"], name


def test_composites_reuse_preprocessing_and_evaluation(built):
    store, config_root, _, _ = built
    processed = build_factor_processed(store, factor_ids=["composite_equal", "composite_icir_60"],
                                       merge_existing=True, config_root=config_root)
    relation = store.output_relation("l3", "factor_processed")
    ids = {row["factor_id"] for row in store.rows(f"SELECT DISTINCT factor_id FROM {relation}")}
    assert {"rev_20", "vol_20", "composite_equal", "composite_icir_60"} <= ids
    assert processed["rows"] > 0
    evaluated = evaluate_factors(store, factor_ids=["composite_equal", "composite_icir_60"],
                                 process_ids=["ws_z"], horizons=[1], config_root=config_root)
    assert evaluated["factor_summary"]["rows"] > 0
    summary = store.output_relation("l4", "factor_summary")
    evaluated_ids = {row["factor_id"] for row in store.rows(f"SELECT DISTINCT factor_id FROM {summary}")}
    assert evaluated_ids == {"composite_equal", "composite_icir_60"}
    evaluate_factors(store, factor_ids=["composite_equal"], process_ids=["ws_z"], horizons=[1],
                     config_root=config_root, merge_existing=True)
    summary = store.output_relation("l4", "factor_summary")
    merged_ids = {row["factor_id"] for row in store.rows(f"SELECT DISTINCT factor_id FROM {summary}")}
    assert merged_ids == {"composite_equal", "composite_icir_60"}
