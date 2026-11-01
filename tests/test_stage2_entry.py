"""Stage 2 admission gate: upstream binding, tamper detection, static guard."""
import json

import pytest

from core.calendar import build_calendar
from core.config import Paths
from core.data.profile import profile_all
from core.data.store import Store
from core.factor.entry import check_no_raw_access, check_stage2_entry
from core.financial.available import build_financial_available
from core.financial.quarters import build_quarterly_and_ttm
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.quality.checks import TABLES, check_data_quality
from core.universe.research import build_research_universe


def _write_passing_acceptance_evidence(store):
    for name in ("raw_unchanged", "repeatability"):
        store.write_json(("verification", name + ".json"), {"passed": True, "method": "golden_fixture_stub"})


@pytest.fixture
def admitted_store(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        core = sorted(table for table, spec in store.tables.items() if spec.get("stage1"))
        profile_all(store, core)
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
        build_trade_status(store)
        build_financial_available(store)
        build_quarterly_and_ttm(store)
        build_research_universe(store)
        _write_passing_acceptance_evidence(store)
        report = check_data_quality(store, l0_scope="core", verify_files=True)
        assert report["passed"] is True
        assert report["acceptance_evidence"]["raw_unchanged"]["passed"] is True
        assert report["acceptance_evidence"]["repeatability"]["passed"] is True
        yield store


def test_passing_stage1_state_yields_a_bound_upstream_bundle(admitted_store):
    result = check_stage2_entry(admitted_store)
    assert result["passed"] is True
    assert result["failures"] == []
    assert set(result["upstream_tables"]) == set(TABLES)
    for name, (layer, key, _required) in TABLES.items():
        entry = result["upstream_tables"][name]
        assert entry["layer"] == layer
        assert entry["primary_key"] == key
        assert entry["sha256"]
    assert result["bundle_sha256"]
    written = json.loads(admitted_store.paths.output("verification", "stage2_entry.json").read_text(encoding="utf-8"))
    assert written == result


def test_bundle_hash_is_deterministic_across_repeated_checks(admitted_store):
    first = check_stage2_entry(admitted_store)
    second = check_stage2_entry(admitted_store)
    assert first["bundle_sha256"] == second["bundle_sha256"]
    assert first["upstream_tables"] == second["upstream_tables"]


def test_missing_quality_report_blocks_entry(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        with pytest.raises(ValueError, match="Stage 2 entry gate failed"):
            check_stage2_entry(store)
        written = json.loads(store.paths.output("verification", "stage2_entry.json").read_text(encoding="utf-8"))
        assert written["passed"] is False
        assert any(f["check"] == "missing_report" for f in written["failures"])


def test_missing_acceptance_evidence_blocks_entry(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        core = sorted(table for table, spec in store.tables.items() if spec.get("stage1"))
        profile_all(store, core)
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
        build_trade_status(store)
        build_financial_available(store)
        build_quarterly_and_ttm(store)
        build_research_universe(store)
        # No raw_unchanged.json / repeatability.json written: Stage 1's own
        # supported-scope report can still pass without them.
        report = check_data_quality(store, l0_scope="core", verify_files=True)
        assert report["passed"] is True
        with pytest.raises(ValueError):
            check_stage2_entry(store)
        written = json.loads(store.paths.output("verification", "stage2_entry.json").read_text(encoding="utf-8"))
        assert written["passed"] is False
        reasons = {f["check"] for f in written["failures"]}
        assert "acceptance_evidence_raw_unchanged_not_passed" in reasons
        assert "acceptance_evidence_repeatability_not_passed" in reasons


def test_tampered_published_file_is_detected_before_binding(admitted_store):
    target = admitted_store.paths.output("l1", "daily_calendar", "part-00000.parquet")
    original = target.read_bytes()
    target.write_bytes(original + b"\x00")
    try:
        with pytest.raises(ValueError, match="Stage 2 entry gate failed"):
            check_stage2_entry(admitted_store)
        written = json.loads(admitted_store.paths.output("verification", "stage2_entry.json").read_text(encoding="utf-8"))
        failure = next(f for f in written["failures"] if f["table"] == "daily_calendar")
        assert failure["check"] == "file_sha256_mismatch"
        assert "daily_calendar" not in written["upstream_tables"]
    finally:
        target.write_bytes(original)


def test_missing_upstream_artifact_names_the_table(admitted_store):
    manifest_path = admitted_store.paths.output("l1", "ttm_financial", "manifest.json")
    saved = manifest_path.read_bytes()
    manifest_path.unlink()
    try:
        with pytest.raises(ValueError, match="Stage 2 entry gate failed"):
            check_stage2_entry(admitted_store)
        written = json.loads(admitted_store.paths.output("verification", "stage2_entry.json").read_text(encoding="utf-8"))
        failure = next(f for f in written["failures"] if f["table"] == "ttm_financial")
        assert failure["check"] == "missing_artifact"
    finally:
        manifest_path.write_bytes(saved)


def test_no_raw_access_guard_is_clean_before_stage2_modules_exist():
    assert check_no_raw_access() == []
