from pathlib import Path

import pytest

from core.strategy.inputs import BoundedCalendar, SnapshotInputs, bounded_calendar
from core.strategy.contract import load_stage3_contract


ROOT = Path(__file__).parent / "fixtures/golden_stage3"


def test_bounded_calendar_preserves_original_index_and_closes_next_link():
    rows = [{"trade_date": f"2020-01-0{index}", "trade_index": index, "next_trade_date": "2025-01-02"}
            for index in range(1, 4)]
    result = bounded_calendar(rows, "20241231")
    assert [row["trade_index"] for row in result] == [1, 2, 3]
    assert result[-1]["next_trade_date"] is None
    calendar = BoundedCalendar(rows, "20241231")
    assert calendar.next("2020-01-02").isoformat() == "2020-01-03"
    with pytest.raises(PermissionError, match="authorized next"):
        calendar.next("2020-01-03")
    with pytest.raises(PermissionError):
        calendar.offset("2020-01-03", 1)


def test_oos_dates_are_not_silently_included_in_bounded_calendar():
    rows = [{"trade_date": "2024-12-31", "trade_index": 1},
            {"trade_date": "2025-01-02", "trade_index": 2}]
    result = bounded_calendar(rows, "20241231")
    assert len(result) == 1 and result[0]["next_trade_date"] is None


def test_fixture_signal_input_and_research_domain_are_separate():
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        signal = inputs.relation("l3/factor_processed")
        assert inputs.db.execute(f"SELECT count(*) FROM {signal}").fetchone()[0] > 0
        with pytest.raises(PermissionError, match="domain"):
            inputs.relation("l3/forward_return")
        research = inputs.relation("l3/forward_return", "research_only")
        assert inputs.db.execute(f"SELECT count(*) FROM {research}").fetchone()[0] == 4
        calendar = inputs.calendar()
        assert calendar.indices[calendar.dates[20]] == 20


def test_outside_namespace_is_rejected(tmp_path):
    with pytest.raises(PermissionError, match="namespace"):
        SnapshotInputs(tmp_path)


def test_input_unit_contract_does_not_claim_real_share_readiness():
    import yaml
    spec = yaml.safe_load(Path("configs/strategy_inputs.yaml").read_text(encoding="utf-8"))
    assert spec["units"]["amount_to_cny_multiplier"] == 1000
    assert spec["units"]["vol_unit_status"] == "not_validated"
    assert spec["capabilities"]["real_share_account_ready"] is False
    assert spec["capabilities"]["company_action_ledger"] is False


def test_snapshot_hash_detects_tampering_before_first_read():
    SnapshotInputs.clear_verification_cache()
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        entry = inputs.manifest["tables"]["l1/daily_calendar"]
        entry["sha256"] = "bad"
        with pytest.raises(ValueError, match="hash mismatch"):
            inputs.relation("l1/daily_calendar")


def test_snapshot_content_is_hashed_once_per_bundle_and_never_in_fast(monkeypatch):
    from core.strategy import inputs as input_module
    SnapshotInputs.clear_verification_cache()
    original = input_module.file_sha256
    calls = []
    monkeypatch.setattr(input_module, "file_sha256", lambda path: calls.append(path) or original(path))
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        inputs.relation("l1/daily_calendar")
        verified_calls = len(calls)
        assert verified_calls == len(inputs.manifest["tables"])
        inputs.relation("l1/daily_calendar")
        assert len(calls) == verified_calls
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        inputs.relation("l1/daily_calendar")
        assert len(calls) == verified_calls
    SnapshotInputs.clear_verification_cache()
    with SnapshotInputs.from_fixture(ROOT, verification="fast") as inputs:
        inputs.relation("l1/daily_calendar")
        assert len(calls) == verified_calls


def test_full_stage2_artifacts_are_not_snapshot_proof():
    contract = load_stage3_contract()["config"]
    assert contract["permissions"]["no_full_calendar_or_full_file_hash_as_proof"] is True
    assert contract["delivery"]["current_input_status"] == "admitted_physical_snapshot"
    assert contract["delivery"]["active_input_bundle"] == "in_sample_65efe5cadd765004"


def test_snapshot_admission_passes_and_rejects_oos_on_fictional_bundle(tmp_path):
    import csv
    import json
    from core.factor.registry import factor_map
    from core.strategy.admission import check_snapshot
    from core.strategy.contract import file_sha256
    from core.strategy.inputs import load_input_spec
    root = tmp_path / "strategy_research/inputs/test_bundle"
    tables = {
        "l1/daily_calendar": {"key": ["trade_date"], "rows": [{"trade_date": "2024-12-31", "trade_index": 1, "next_trade_date": None}]},
        "l1/daily_adjusted_price": {"key": ["trade_date", "ts_code"], "rows": [{"trade_date": "2024-12-31", "ts_code": "SA001.SZ"}]},
        "l1/daily_stock_state": {"key": ["trade_date", "ts_code"], "rows": [{"trade_date": "2024-12-31", "ts_code": "SA001.SZ"}]},
        "l1/daily_trade_status": {"key": ["trade_date", "ts_code"], "rows": [{"trade_date": "2024-12-31", "ts_code": "SA001.SZ"}]},
        "l1/financial_available": {"key": ["source_table", "record_id"], "rows": [{"source_table": "income_vip", "record_id": "1", "available_date": "2024-12-31", "effective_ann_date": "2024-12-30"}]},
        "l1/quarterly_financial": {"key": ["source_table", "ts_code", "metric", "end_date", "available_date"], "rows": [{"source_table": "income_vip", "ts_code": "SA001.SZ", "metric": "x", "end_date": "2024-09-30", "available_date": "2024-12-31"}]},
        "l1/ttm_financial": {"key": ["source_table", "ts_code", "metric", "end_date", "available_date"], "rows": [{"source_table": "income_vip", "ts_code": "SA001.SZ", "metric": "x", "end_date": "2024-09-30", "available_date": "2024-12-31"}]},
        "l2/daily_research_universe": {"key": ["trade_date", "ts_code", "universe_id"], "rows": [{"trade_date": "2024-12-31", "ts_code": "SA001.SZ", "universe_id": "all_a_ex_st_ipo120_ex_bj"}]},
        "l3/daily_industry": {"key": ["trade_date", "ts_code"], "rows": [{"trade_date": "2024-12-31", "ts_code": "SA001.SZ"}]},
        "l3/daily_index_member": {"key": ["trade_date", "index_code", "con_code"], "rows": [{"trade_date": "2024-12-31", "index_code": "X", "con_code": "SA001.SZ", "available_date": "2024-12-31"}]},
        "l3/daily_benchmark_return": {"key": ["trade_date", "benchmark_id", "method"], "rows": [{"trade_date": "2024-12-31", "benchmark_id": "X", "method": "circ_mv"}]},
        "l4/factor_composite_weight": {"key": ["trade_date", "composite_id", "component_id"], "rows": [{"trade_date": "2024-12-31", "composite_id": "composite_equal", "component_id": "bp", "last_signal_date": "2024-12-01", "last_exit_date": "2024-12-02"}]},
        "l3/forward_return": {"key": ["trade_date", "ts_code", "label_id"], "rows": [{"trade_date": "2024-12-31", "ts_code": "SA001.SZ", "label_id": "ret_o2o_5", "entry_date": None, "exit_date": None, "ret": None, "entry_price": None, "exit_price": None, "input_scope_status": "quarantined"}]},
    }
    factor_rows = [{"trade_date": "2024-12-31", "ts_code": "SA001.SZ", "factor_id": factor_id} for factor_id in factor_map(Path("configs"))]
    tables["l3/factor_value"] = {"key": ["trade_date", "ts_code", "factor_id"], "rows": factor_rows}
    tables["l3/factor_processed"] = {"key": ["trade_date", "ts_code", "factor_id", "universe_id", "process_id"], "rows": [dict(row, universe_id="all_a_ex_st_ipo120_ex_bj", process_id="ws_z") for row in factor_rows]}
    manifest = {"status": "complete", "cutoff": "20241231", "tables": {}}
    for artifact, spec in tables.items():
        domain = "research_only" if artifact == "l3/forward_return" else "signal_inputs"
        name = artifact.split("/")[1]
        path = root / domain / name / "part-00000.csv"
        path.parent.mkdir(parents=True)
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(spec["rows"][0]))
            writer.writeheader()
            writer.writerows(spec["rows"])
        manifest["tables"][artifact] = {"domain": domain, "path": path.relative_to(root).as_posix(), "rows": len(spec["rows"]), "sha256": file_sha256(path)}
    root.joinpath("manifest.json").write_text(json.dumps(manifest))
    result = check_snapshot(root, tmp_path, spec=load_input_spec(Path.cwd()), registry_root=Path.cwd() / "configs")
    assert result["passed"], result
    assert result["observations"]["factor_value_ids"] == 20
    path = root / manifest["tables"]["l1/daily_calendar"]["path"]
    path.write_text("trade_date,trade_index,next_trade_date\n2025-01-02,2,2025-01-03\n")
    result = check_snapshot(root, tmp_path, spec=load_input_spec(Path.cwd()), registry_root=Path.cwd() / "configs", verification="audit")
    assert not result["passed"]
    assert any(failure["check"] == "input_access" and "hash mismatch" in failure["detail"] for failure in result["failures"])
