"""End-to-end quality gates using built artificial artifacts and targeted corruption."""
import json
import hashlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from core.calendar import build_calendar
from core.config import Paths
from core.data.profile import profile_all
from core.data.store import Store, qs
from core.financial.available import build_financial_available
from core.financial.quarters import build_quarterly_and_ttm
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.quality.checks import check_data_quality
from core.universe.research import build_research_universe


@pytest.fixture
def quality_store(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        core = sorted(table for table, spec in store.tables.items() if spec.get("stage1"))
        assert len(core) == 9
        profile_all(store, core)
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
        build_trade_status(store)
        build_financial_available(store)
        build_quarterly_and_ttm(store)
        build_research_universe(store)
        yield store


def _failed_report(store, *, verify_files=False):
    with pytest.raises(ValueError):
        check_data_quality(store, l0_scope="core", verify_files=verify_files)
    path = store.paths.output("l2", "data_quality_report", "report.json")
    assert path.is_file()
    assert store.paths.output("l2", "data_quality_report", "REPORT.md").is_file()
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["failures"]
    for failure in report["failures"]:
        assert {"table", "check", "fields", "samples"} <= failure.keys()
    return report


def _assert_context(report, table, field, *, code=None, day=None):
    matching = [failure for failure in report["failures"] if failure["table"] == table]
    assert matching, report["failures"]
    serialized = json.dumps(matching, ensure_ascii=False)
    assert field in serialized
    if code:
        assert code in serialized
    if day:
        assert day in serialized


def test_complete_golden_pipeline_passes_supported_scope_and_reports_deferrals(quality_store):
    report = check_data_quality(quality_store, l0_scope="core", verify_files=True)
    assert report["passed"] is True
    assert report["full_phase_passed"] is False
    assert report["failures"] == []
    assert report["capabilities"]["historical_st"]["status"] == "deferred"
    assert report["capabilities"]["historical_st"]["reason"]
    for table in ("daily_calendar", "daily_adjusted_price", "daily_stock_state", "daily_trade_status",
                  "financial_available", "quarterly_financial", "ttm_financial", "daily_research_universe",
                  "research_universe_config"):
        profile = report["tables"][table]
        assert {"schema", "rows", "null_counts", "date_ranges", "primary_key", "fingerprint", "sha256"} <= profile.keys()
        assert profile["rows"] > 0
    config = quality_store.output_relation("l2", "research_universe_config")
    assert quality_store.db.execute(f"SELECT count(*) FROM {config} WHERE status='deferred'").fetchone()[0] == 3
    assert quality_store.paths.output("l2", "data_quality_report", "report.json").is_file()
    assert quality_store.paths.output("l2", "data_quality_report", "REPORT.md").is_file()


def test_return_across_a_missing_trading_day_is_reported_with_security_and_date(quality_store):
    source = quality_store.output_relation("l1", "daily_adjusted_price")
    quality_store.publish(
        "l1", "daily_adjusted_price",
        f"SELECT * REPLACE (CASE WHEN ts_code='000001.SZ' AND trade_date=DATE '2019-01-10' "
        f"THEN 0.25 ELSE ret_1d END AS ret_1d) FROM {source}",
        ["trade_date", "ts_code"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "daily_adjusted_price", "ret_1d", code="000001.SZ", day="2019-01-10")


def test_finite_but_wrong_consecutive_day_return_is_rejected(quality_store):
    source = quality_store.output_relation("l1", "daily_adjusted_price")
    quality_store.publish(
        "l1", "daily_adjusted_price",
        f"SELECT * REPLACE (CASE WHEN ts_code='000001.SZ' AND trade_date=DATE '2019-01-07' "
        f"THEN 0.25 ELSE ret_1d END AS ret_1d) FROM {source}",
        ["trade_date", "ts_code"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "daily_adjusted_price", "ret_1d", code="000001.SZ", day="2019-01-07")


def test_consistently_changed_raw_and_adjusted_prices_still_must_match_raw_source(quality_store):
    source = quality_store.output_relation("l1", "daily_adjusted_price")
    condition = "ts_code='000001.SZ' AND trade_date=DATE '2019-01-08'"
    replacements = [f"CASE WHEN {condition} THEN 50 ELSE {field} END AS {field}" for field in ("open", "high", "low", "close")]
    replacements += [f"CASE WHEN {condition} THEN 100 ELSE adjusted_{field} END AS adjusted_{field}" for field in ("open", "high", "low", "close")]
    replacements += [f"CASE WHEN {condition} THEN 100.0/99-1 ELSE ret_1d END AS ret_1d"]
    quality_store.publish(
        "l1", "daily_adjusted_price", f"SELECT * REPLACE ({','.join(replacements)}) FROM {source}",
        ["trade_date", "ts_code"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "daily_adjusted_price", "close", code="000001.SZ", day="2019-01-08")


def test_current_upstream_fingerprint_must_match_downstream_provenance(quality_store):
    path = quality_store.paths.output("l1", "daily_calendar", "part-00000.parquet")
    original = pq.ParquetFile(path).read()
    metadata = dict(original.schema.metadata or {})
    metadata[b"quality_test_marker"] = b"new upstream publication"
    pq.write_table(original.replace_schema_metadata(metadata), path)
    manifest_path = quality_store.paths.output("l1", "daily_calendar", "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert new_hash != manifest["sha256"]
    manifest["sha256"] = new_hash
    quality_store.write_json(("l1", "daily_calendar", "manifest.json"), manifest)
    report = _failed_report(quality_store, verify_files=True)
    _assert_context(report, "daily_adjusted_price", "upstream")
    assert "daily_calendar" in json.dumps(report["failures"])


def test_missing_l0_profile_fails_with_a_saved_report(quality_store):
    quality_store.paths.output("l0", "profiles", "stock_basic.json").unlink()
    report = _failed_report(quality_store)
    assert "stock_basic" in json.dumps(report["failures"], ensure_ascii=False)


def test_duplicate_output_key_reports_its_date_and_security(quality_store):
    path = quality_store.paths.output("l1", "daily_adjusted_price", "part-00000.parquet")
    original = pq.ParquetFile(path).read()
    pq.write_table(pa.concat_tables([original, original.slice(0, 1)]), path)
    report = _failed_report(quality_store)
    _assert_context(report, "daily_adjusted_price", "ts_code", code="000001.SZ", day="2018-12-17")


@pytest.mark.parametrize("corruption,field", [("missing_field", "adjusted_close"), ("wrong_type", "trade_date")])
def test_output_schema_corruption_is_reported_before_query_failure(quality_store, corruption, field):
    path = quality_store.paths.output("l1", "daily_adjusted_price", "part-00000.parquet")
    original = pq.ParquetFile(path).read()
    if corruption == "missing_field":
        changed = original.drop_columns([field])
    else:
        index = original.schema.get_field_index(field)
        changed = original.set_column(index, field, original[field].cast(pa.string()))
    pq.write_table(changed, path)
    report = _failed_report(quality_store)
    _assert_context(report, "daily_adjusted_price", field)


def test_financial_record_cannot_be_visible_on_its_announcement_day(quality_store):
    relation = quality_store.output_relation("l1", "financial_available")
    quality_store.publish(
        "l1", "financial_available",
        f"SELECT * REPLACE (CASE WHEN source_table='income_vip' AND ts_code='000001.SZ' "
        f"AND end_date=DATE '2018-03-31' THEN ann_date ELSE available_date END AS available_date) FROM {relation}",
        ["source_table", "record_id"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "financial_available", "available_date", code="000001.SZ", day="2018-12-20")


def test_consistent_financial_date_mapping_must_still_match_preserved_raw_dates(quality_store):
    relation = quality_store.output_relation("l1", "financial_available")
    condition = "source_table='balancesheet_vip' AND ts_code='000001.SZ' AND end_date=DATE '2018-03-31'"
    replacements = [f"CASE WHEN {condition} THEN DATE '2018-12-19' ELSE {field} END AS {field}"
                    for field in ("ann_date", "f_ann_date", "effective_ann_date")]
    replacements.append(f"CASE WHEN {condition} THEN DATE '2018-12-20' ELSE available_date END AS available_date")
    quality_store.publish(
        "l1", "financial_available", f"SELECT * REPLACE ({','.join(replacements)}) FROM {relation}",
        ["source_table", "record_id"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "financial_available", "ann_date", code="000001.SZ", day="2018-12-19")


def test_valid_ttm_requires_four_actual_quarter_lineage_ids(quality_store):
    relation = quality_store.output_relation("l1", "ttm_financial")
    quality_store.publish(
        "l1", "ttm_financial",
        f"SELECT * REPLACE (CASE WHEN source_table='income_vip' AND ts_code='000001.SZ' "
        f"AND metric='revenue' AND end_date=DATE '2018-12-31' AND available_date=DATE '2019-01-07' "
        f"THEN list_slice(source_quarter_ids,1,3) ELSE source_quarter_ids END AS source_quarter_ids) FROM {relation}",
        ["source_table", "ts_code", "metric", "end_date", "available_date"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "ttm_financial", "source_quarter_ids", code="000001.SZ", day="2018-12-31")


def test_ttm_cannot_reference_a_future_quarter_revision(quality_store):
    quarters = quality_store.output_relation("l1", "quarterly_financial")
    future_id = quality_store.db.execute(
        f"SELECT version_id FROM {quarters} WHERE source_table='income_vip' AND ts_code='000001.SZ' "
        "AND metric='revenue' AND end_date=DATE '2018-06-30' AND available_date=DATE '2019-01-14'"
    ).fetchone()[0]
    relation = quality_store.output_relation("l1", "ttm_financial")
    quality_store.publish(
        "l1", "ttm_financial",
        f"SELECT * REPLACE (CASE WHEN source_table='income_vip' AND ts_code='000001.SZ' "
        f"AND metric='revenue' AND end_date=DATE '2018-12-31' AND available_date=DATE '2019-01-07' "
        f"THEN [source_quarter_ids[1],{qs(future_id)},source_quarter_ids[3],source_quarter_ids[4]] "
        f"ELSE source_quarter_ids END AS source_quarter_ids) FROM {relation}",
        ["source_table", "ts_code", "metric", "end_date", "available_date"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "ttm_financial", "source_quarter_ids", code="000001.SZ", day="2019-01-07")


def test_ttm_cannot_reuse_an_obsolete_quarter_when_a_newer_revision_is_visible(quality_store):
    quarters = quality_store.output_relation("l1", "quarterly_financial")
    old_id = quality_store.db.execute(
        f"SELECT version_id FROM {quarters} WHERE source_table='income_vip' AND ts_code='000001.SZ' "
        "AND metric='revenue' AND end_date=DATE '2018-06-30' AND available_date=DATE '2018-12-24'"
    ).fetchone()[0]
    relation = quality_store.output_relation("l1", "ttm_financial")
    condition = ("source_table='income_vip' AND ts_code='000001.SZ' AND metric='revenue' "
                 "AND end_date=DATE '2018-12-31' AND available_date=DATE '2019-01-14'")
    changed_quarters = quality_store.db.execute(
        f"SELECT source_quarter_ids FROM {relation} WHERE {condition}"
    ).fetchone()[0]
    changed_quarters[1] = old_id
    changed_records = [row[0] for row in quality_store.db.execute(
        f"SELECT DISTINCT unnest(source_record_ids) AS record_id FROM {quarters} "
        f"WHERE version_id IN ({','.join(qs(i) for i in changed_quarters)}) ORDER BY record_id"
    ).fetchall()]
    quality_store.publish(
        "l1", "ttm_financial",
        f"SELECT * REPLACE (CASE WHEN {condition} "
        f"THEN [{','.join(qs(i) for i in changed_quarters)}] "
        f"ELSE source_quarter_ids END AS source_quarter_ids, "
        f"CASE WHEN {condition} THEN [{','.join(qs(i) for i in changed_records)}] "
        f"ELSE source_record_ids END AS source_record_ids, "
        f"CASE WHEN {condition} THEN value-2 ELSE value END AS value) FROM {relation}",
        ["source_table", "ts_code", "metric", "end_date", "available_date"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "ttm_financial", "source_quarter_ids", code="000001.SZ", day="2019-01-14")


def test_quarter_lineage_must_reference_the_correct_report_period_even_on_same_visible_day(quality_store):
    financial = quality_store.output_relation("l1", "financial_available")
    wrong_ids = [row[0] for row in quality_store.db.execute(
        f"SELECT record_id FROM {financial} WHERE source_table='income_vip' AND ts_code='000001.SZ' "
        "AND end_date IN (DATE '2018-03-31',DATE '2018-09-30') ORDER BY record_id"
    ).fetchall()]
    assert len(wrong_ids) == 2
    relation = quality_store.output_relation("l1", "quarterly_financial")
    condition = ("source_table='income_vip' AND ts_code='000001.SZ' AND metric='revenue' "
                 "AND end_date=DATE '2018-06-30' AND available_date=DATE '2018-12-24'")
    assert quality_store.db.execute(f"SELECT count(*) FROM {relation} WHERE {condition}").fetchone()[0] == 1
    quality_store.publish(
        "l1", "quarterly_financial",
        f"SELECT * REPLACE (CASE WHEN {condition} THEN [{','.join(qs(i) for i in wrong_ids)}] "
        f"ELSE source_record_ids END AS source_record_ids) FROM {relation}",
        ["source_table", "ts_code", "metric", "end_date", "available_date"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "quarterly_financial", "source_record_ids", code="000001.SZ", day="2018-06-30")
    assert any(f["table"] == "quarterly_financial" and f["check"] == "latest_visible_source_versions"
               for f in report["failures"])


def test_coherently_changed_quarter_and_ttm_amounts_still_must_match_cumulative_source(quality_store):
    relation = quality_store.output_relation("l1", "quarterly_financial")
    condition = ("source_table='income_vip' AND ts_code='000001.SZ' AND metric='revenue' "
                 "AND end_date=DATE '2018-03-31' AND available_date=DATE '2018-12-21'")
    changed_id, old_value = quality_store.db.execute(
        f"SELECT version_id,value FROM {relation} WHERE {condition}"
    ).fetchone()
    assert old_value == 10
    quality_store.publish(
        "l1", "quarterly_financial",
        f"SELECT * REPLACE (CASE WHEN {condition} THEN value+1 ELSE value END AS value) FROM {relation}",
        ["source_table", "ts_code", "metric", "end_date", "available_date"],
    )
    quality_store.output_relation("l1", "quarterly_financial")
    ttm = quality_store.output_relation("l1", "ttm_financial")
    ttm_condition = f"status='available' AND list_contains(source_quarter_ids,{qs(changed_id)})"
    assert quality_store.db.execute(f"SELECT count(*) FROM {ttm} WHERE {ttm_condition}").fetchone()[0] == 2
    quality_store.publish(
        "l1", "ttm_financial",
        f"SELECT * REPLACE (CASE WHEN {ttm_condition} THEN value+1 ELSE value END AS value) FROM {ttm}",
        ["source_table", "ts_code", "metric", "end_date", "available_date"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "quarterly_financial", "value", code="000001.SZ", day="2018-03-31")
    assert any(f["table"] == "quarterly_financial" and f["check"] == "cumulative_difference_value"
               for f in report["failures"])
    assert not any(f["table"] == "ttm_financial" and f["check"] == "quarter_lineage_and_sum"
                   for f in report["failures"])


def test_later_delisted_security_remains_in_its_earlier_identity_sample(quality_store):
    relation = quality_store.output_relation("l2", "daily_research_universe")
    condition = "universe_id='all_a' AND ts_code='600001.SH' AND trade_date=DATE '2019-01-22'"
    quality_store.publish(
        "l2", "daily_research_universe",
        f"SELECT * REPLACE (CASE WHEN {condition} THEN false ELSE is_in_universe END AS is_in_universe, "
        f"CASE WHEN {condition} THEN 'CURRENTLY_DELISTED' ELSE exclusion_reason END AS exclusion_reason) FROM {relation}",
        ["trade_date", "ts_code", "universe_id"],
    )
    report = _failed_report(quality_store)
    _assert_context(report, "daily_research_universe", "is_in_universe", code="600001.SH", day="2019-01-22")


def test_output_file_fingerprint_detects_valid_parquet_metadata_tampering(quality_store):
    path = quality_store.paths.output("l1", "daily_adjusted_price", "part-00000.parquet")
    original = pq.ParquetFile(path).read()
    metadata = dict(original.schema.metadata or {})
    metadata[b"quality_test_marker"] = b"tampered without changing logical rows"
    pq.write_table(original.replace_schema_metadata(metadata), path)
    report = _failed_report(quality_store, verify_files=True)
    failures = [failure for failure in report["failures"] if failure["table"] == "daily_adjusted_price"]
    assert failures
    text = json.dumps(failures).lower()
    assert "sha256" in text or "fingerprint" in text or "hash" in text
