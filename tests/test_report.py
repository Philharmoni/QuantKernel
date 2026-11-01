"""Step 9 report reconciliation, traceability and deterministic file generation."""
from datetime import date
import csv
import json
import shutil

import pyarrow as pa
import pytest

from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store
from core.evaluation.report import build_factor_reports
from core.evaluation.trial import append_trial_log


def _configs(tmp_path):
    destination = tmp_path / "configs"
    destination.mkdir()
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml",
                     "factor_research.yaml", "factor_registry.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / filename, destination / filename)
    return destination


def _publish(store, layer, name, rows, key):
    frame = pa.Table.from_pylist(rows)
    store.db.register(f"{name}_input", frame)
    result = store.publish(layer, name, f"SELECT * FROM {name}_input", key)
    store.db.unregister(f"{name}_input")
    return result


def _summary_row(horizon=5, process_id="ws_z", period_id="in_sample", segment_id="full"):
    return {
        "factor_id": "rev_20", "universe_id": "all_a_ex_st_ipo120_ex_bj",
        "process_id": process_id, "label_id": f"ret_o2o_{horizon}",
        "sample_mode": "theoretical", "segment_id": segment_id, "period_id": period_id,
        "rank_ic_n_days": 100, "ic_mean": 0.03, "ic_std": 0.1,
        "rank_ic_mean": 0.05, "annualized_rank_icir": 1.2, "rank_ic_t_stat": 2.3,
        "factor_rank_autocorrelation": 0.9, "group1_annualized_return": 0.08,
        "group1_sharpe": 0.7, "group1_max_drawdown": -0.05,
        "group1_mean_one_way_turnover": 0.2, "group1_breakeven_cost": 0.01,
        "long_short_annualized_return": 0.12, "long_short_sharpe": 1.1,
        "long_short_max_drawdown": -0.06, "long_short_calmar": 2.0,
        "long_short_mean_one_way_turnover": 0.4, "long_short_breakeven_cost": 0.02,
        "monotonicity": 0.8,
    }


@pytest.fixture
def report_store(tmp_path):
    store = Store(Paths(PROJECT_ROOT / "tests" / "fixtures" / "golden_stage2",
                        tmp_path / "middle"), config_root=_configs(tmp_path))
    day = date(2020, 1, 2)
    _publish(store, "l3", "factor_value",
             [{"trade_date": day, "ts_code": "SA001.SZ", "factor_id": "rev_20", "value": 1.0}],
             ["trade_date", "ts_code", "factor_id"])
    _publish(store, "l3", "factor_processed",
             [{"trade_date": day, "ts_code": "SA001.SZ", "factor_id": "rev_20",
               "universe_id": "all_a_ex_st_ipo120_ex_bj", "process_id": "ws_z", "value": 1.0}],
             ["trade_date", "ts_code", "factor_id", "universe_id", "process_id"])
    _publish(store, "l4", "factor_ic_daily", [{"factor_id": "rev_20", "rank_ic": 0.05}], ["factor_id"])
    _publish(store, "l4", "factor_quantile_return", [{"factor_id": "rev_20", "ret": 0.01}], ["factor_id"])
    rows = [_summary_row(horizon=h) for h in (1, 5, 10, 20)]
    rows += [_summary_row(process_id="ws_z_industry_mv_neutral_z"),
             _summary_row(period_id="year:2020"),
             _summary_row(period_id="cap_1", segment_id="cap_1")]
    _publish(store, "l4", "factor_summary", rows,
             ["factor_id", "universe_id", "process_id", "label_id", "sample_mode",
              "segment_id", "period_id"])
    _publish(store, "l4", "factor_correlation",
             [{"universe_id": "all_a_ex_st_ipo120_ex_bj", "process_id": "ws_z",
               "factor_a": "rev_20", "factor_b": "vol_20", "n_days": 100,
               "mean_correlation": -0.4, "mean_abs_correlation": 0.4, "redundant": False}],
             ["universe_id", "process_id", "factor_a", "factor_b"])
    store.write_json(("l3", "factor_value", "coverage_report.json"),
                     {"summary": [{"factor_id": "rev_20", "values": 1000, "days": 100,
                                   "mean_coverage": 0.95, "min_coverage": 0.8}]})
    store.write_json(("l3", "factor_processed", "processing_report.json"),
                     {"summary": [{"factor_id": "rev_20", "valid_values": 990,
                                   "skipped_days": 1, "neutralize_skipped_days": 0,
                                   "max_unknown_industry_share": 0.1}]})
    append_trial_log(store, [{"factor_id": "rev_20", "factor_version": 1,
                              "evaluation_start": "20200101", "evaluation_end": "20241231",
                              "oos_accessed": False}])
    yield store
    store.close()


def test_reports_reconcile_and_are_traceable(report_store):
    result = build_factor_reports(report_store, factor_ids=["rev_20"])
    output = report_store.paths.output("l4", "factor_report")
    payload = json.loads((output / "rev_20.json").read_text(encoding="utf-8"))
    markdown = (output / "rev_20.md").read_text(encoding="utf-8")
    assert result["factors"] == 1 and result["files"] == 5
    assert len(payload["ic_decay"]) == 4
    assert [row["label_id"] for row in payload["quantile_performance"]] == [
        "ret_o2o_1", "ret_o2o_5", "ret_o2o_10", "ret_o2o_20"]
    assert len(payload["annual"]) == 1 and len(payload["segments"]) == 1
    assert payload["correlations"][0]["peer"] == "vol_20"
    assert payload["sample_status"] == "in_sample_only"
    assert "0.050000" in markdown and payload["trial"]["trial_id"] in markdown
    assert "尚未正式访问锁定样本外" in markdown
    summary = list(csv.DictReader((output / "summary.csv").open(encoding="utf-8")))
    assert len(summary) == 1 and summary[0]["h5_rank_ic_mean"] == "0.05"
    assert payload["sources"]["l4/factor_summary"]["sha256"] == report_store._upstream["l4/factor_summary"]


def test_report_rebuild_has_identical_file_hashes(report_store):
    first = build_factor_reports(report_store, factor_ids=["rev_20"])["manifest"]
    second = build_factor_reports(report_store, factor_ids=["rev_20"])["manifest"]
    assert first["files"] == second["files"]
    assert first["research_registry_sha256"] == second["research_registry_sha256"]
    assert "logged_at_utc" not in first
