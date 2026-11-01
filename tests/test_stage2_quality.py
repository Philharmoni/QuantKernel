"""Step 10 unified quality-gate components."""
from datetime import date, timedelta
import json

import pyarrow as pa

from core.config import Paths
from core.data.store import Store
from core.evaluation.trial import append_trial_log
from core.quality.stage2 import (check_documentation, check_layering, check_oos_status,
                                 check_pit, check_repeatability)


def _publish(store, layer, name, rows, key):
    store.db.register("quality_input", pa.Table.from_pylist(rows))
    result = store.publish(layer, name, "SELECT * FROM quality_input", key)
    store.db.unregister("quality_input")
    return result


def test_layering_checks_current_file_schema_and_key(tmp_path, golden_stage2_root):
    with Store(Paths(golden_stage2_root, tmp_path / "middle")) as store:
        artifact = {"l3/factor_value": ["trade_date", "ts_code", "factor_id"]}
        _publish(store, "l3", "factor_value",
                 [{"trade_date": "2020-01-02", "ts_code": "SA001.SZ", "factor_id": "rev_20", "value": 1.0}],
                 ["trade_date", "ts_code", "factor_id"])
        result = check_layering(store, artifact, deep_primary_keys=True)
        assert result["passed"] and result["deep_primary_keys"]
        wrong = check_layering(store, {"l3/factor_value": ["factor_id"]})
        assert not wrong["passed"]
        assert wrong["failures"][0]["check"] == "primary_key"


def test_repeatability_compares_stage1_and_step7_to_9_evidence(tmp_path, golden_stage2_root):
    with Store(Paths(golden_stage2_root, tmp_path / "middle")) as store:
        store.write_json(("verification", "repeatability.json"), {"differences": []})
        specs = {"step7": (("_repeat_step7_c", "_repeat_step7_d"),
                           [("l4", name) for name in ("factor_ic_daily", "factor_quantile_return", "factor_summary")]),
                 "step8": (("_repeat_step8_a", "_repeat_step8_b"),
                           [("l4", "factor_correlation"), ("l4", "factor_ic_realized"),
                            ("l4", "factor_composite_weight"), ("l3", "factor_composite_value"),
                            ("l3", "factor_value"), ("l3", "factor_processed")])}
        for _, (roots, artifacts) in specs.items():
            for root in roots:
                for layer, name in artifacts:
                    path = store.paths.middle_root / root / layer / name / "manifest.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"rows": 1, "sha256": "same"}), encoding="utf-8")
        for root in ("_repeat_step9_a", "_repeat_step9_b"):
            path = store.paths.middle_root / root / "l4" / "factor_report" / "manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"files": {}}), encoding="utf-8")
        result = check_repeatability(store)
        assert result["passed"] and len(result["comparisons"]) == 11
        path = store.paths.middle_root / "_repeat_step7_d" / "l4" / "factor_summary" / "manifest.json"
        path.write_text(json.dumps({"rows": 1, "sha256": "different"}), encoding="utf-8")
        assert not check_repeatability(store)["passed"]


def test_documentation_requires_all_eleven_steps(tmp_path):
    path = tmp_path / "STAGE2_EXECUTION.md"
    path.write_text("\n".join(f"## Step {step}：x" for step in range(11)), encoding="utf-8")
    assert check_documentation(path)["passed"]
    path.write_text("## Step 0：x\n## Step 10：x", encoding="utf-8")
    result = check_documentation(path)
    assert not result["passed"] and result["missing_steps"] == list(range(1, 10))


def test_oos_status_reconciles_trials_summary_and_report(tmp_path, golden_stage2_root, monkeypatch):
    with Store(Paths(golden_stage2_root, tmp_path / "middle")) as store:
        monkeypatch.setattr("core.quality.stage2.factor_map",
                            lambda: {"rev_20": {"version": 1}})
        _publish(store, "l4", "factor_summary",
                 [{"factor_id": "rev_20", "period_id": "in_sample"}], ["factor_id", "period_id"])
        append_trial_log(store, [{"factor_id": "rev_20", "factor_version": 1, "oos_accessed": False}])
        store.write_json(("l4", "factor_report", "summary.json"),
                         {"rows": [{"factor_id": "rev_20", "sample_status": "in_sample_only"}]})
        store.write_json(("l4", "factor_report", "manifest.json"), {"sources": {}, "files": {}})
        result = check_oos_status(store)
        assert result["passed"] and result["trial_records"] == 1
        store.write_json(("l4", "factor_report", "summary.json"),
                         {"rows": [{"factor_id": "rev_20", "sample_status": "in_sample_and_out_of_sample"}]})
        assert not check_oos_status(store)["passed"]


def test_pit_label_entry_rules_distinguish_c2c_and_o2o(tmp_path, golden_stage2_root):
    with Store(Paths(golden_stage2_root, tmp_path / "middle")) as store:
        day = date(2020, 1, 2)
        _publish(store, "l1", "financial_available",
                 [{"source_table": "income_vip", "record_id": "1",
                   "effective_ann_date": day, "available_date": day + timedelta(days=1)}],
                 ["source_table", "record_id"])
        _publish(store, "l3", "daily_industry",
                 [{"trade_date": day, "ts_code": "SA001.SZ", "industry_pit_status": "snapshot_after_in_date"}],
                 ["trade_date", "ts_code"])
        _publish(store, "l4", "factor_composite_weight",
                 [{"trade_date": day + timedelta(days=1), "composite_id": "composite_icir_60",
                   "component_id": "rev_20", "last_exit_date": day}],
                 ["trade_date", "composite_id", "component_id"])
        _publish(store, "l4", "factor_ic_realized",
                 [{"signal_date": day, "factor_id": "rev_20", "universe_id": "all_a_ex_st_ipo120_ex_bj",
                   "process_id": "ws_z", "label_id": "ret_o2o_5", "max_exit_date": day + timedelta(days=1)}],
                 ["signal_date", "factor_id", "universe_id", "process_id", "label_id"])

        def label_rows(o2o_entry):
            return [
                {"trade_date": day, "ts_code": "SA001.SZ", "label_id": "ret_c2c_1",
                 "entry_date": day, "exit_date": day + timedelta(days=1), "ret": 0.01,
                 "exception_flag": None, "beyond_data_end": False},
                {"trade_date": day, "ts_code": "SA001.SZ", "label_id": "ret_o2o_1",
                 "entry_date": o2o_entry, "exit_date": day + timedelta(days=2), "ret": 0.01,
                 "exception_flag": None, "beyond_data_end": False},
            ]

        _publish(store, "l3", "forward_return", label_rows(day + timedelta(days=1)),
                 ["trade_date", "ts_code", "label_id"])
        assert check_pit(store)["passed"]
        _publish(store, "l3", "forward_return", label_rows(day), ["trade_date", "ts_code", "label_id"])
        result = check_pit(store)
        assert not result["passed"] and result["observations"]["invalid_actual_interval"] == 1
