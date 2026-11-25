"""Custom Stage 1 member-file universes and their isolated contracts."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from core.calendar import build_calendar
from core.config import Paths
from core.data.store import Store
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.universe.custom import build_custom_research_universe, custom_universe_identity, validate_custom_research_universe
from core.universe.research import build_research_universe


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dependencies(store, formal=False):
    build_calendar(store)
    build_stock_state(store)
    if formal:
        build_adjusted_price(store)
        build_trade_status(store)
        build_research_universe(store)


def test_member_file_builds_historical_names_without_touching_formal_universe(tmp_path, golden_root):
    paths = Paths(golden_root, tmp_path / "middle")
    source = tmp_path / "members.csv"
    source.write_text("trade_date,ts_code,stock_name\n"
                      "20190103,000001.SZ,wrong display name\n"
                      "2019-01-15,000002.SZ,ignored\n"
                      "20190116,000002.SZ,ignored\n", encoding="utf-8")
    with Store(paths) as store:
        _dependencies(store, formal=True)
        formal = paths.output("l2", "daily_research_universe", "part-00000.parquet")
        before = _sha(formal)
        result = build_custom_research_universe(store, source, "custom_pool_v1")
        assert result["universe"]["rows"] == 3
        assert _sha(formal) == before
        members = store.output_relation("l2", "custom_research_universe")
        rows = store.rows(f"SELECT * FROM {members} ORDER BY trade_date,ts_code")
        states = store.output_relation("l1", "daily_stock_state")
        expected = {(row["trade_date"], row["ts_code"]): row["historical_name"]
                    for row in store.rows(f"SELECT * FROM {states}")}
        assert list(rows[0]) == ["trade_date", "ts_code", "stock_name", "universe_id", "is_in_universe"]
        assert all(row["stock_name"] == expected[(row["trade_date"], row["ts_code"])] for row in rows)
        assert rows[0]["stock_name"] != "wrong display name"
        assert rows[1]["stock_name"] != rows[2]["stock_name"]
        assert all(row["is_in_universe"] is True for row in rows)
        assert validate_custom_research_universe(store, "custom_pool_v1")["passed"]
        identity = custom_universe_identity(store, "custom_pool_v1")
        assert identity["config_id"] and identity["members_manifest_sha256"]


def test_multiple_pools_are_immutable_and_parquet_matches_csv(tmp_path, golden_root):
    csv_source = tmp_path / "first.csv"
    csv_source.write_text("trade_date,ts_code\n20190103,000001.SZ\n", encoding="utf-8")
    parquet_source = tmp_path / "second.parquet"
    pq.write_table(pa.Table.from_pylist([{"trade_date": "20190103", "ts_code": "000001.SZ"}]), parquet_source)
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _dependencies(store)
        first = build_custom_research_universe(store, csv_source, "pool_one")
        repeat = build_custom_research_universe(store, csv_source, "pool_one")
        assert repeat["universe"]["sha256"] == first["universe"]["sha256"]
        second = build_custom_research_universe(store, parquet_source, "pool_two")
        before = custom_universe_identity(store, "pool_one")["config_id"]
        members = store.output_relation("l2", "custom_research_universe")
        assert store.rows(f"SELECT universe_id,count(*) AS n FROM {members} GROUP BY 1 ORDER BY 1") == [
            {"universe_id": "pool_one", "n": 1}, {"universe_id": "pool_two", "n": 1}]
        assert second["universe"]["rows"] == 2
        csv_source.write_text("trade_date,ts_code\n20190103,000002.SZ\n", encoding="utf-8")
        with pytest.raises(ValueError, match="immutable"):
            build_custom_research_universe(store, csv_source, "pool_one")
        assert custom_universe_identity(store, "pool_one")["config_id"] == before


def test_duckdb_relation_is_published_directly_without_an_intermediate_file(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _dependencies(store)
        store.db.execute("""CREATE TEMP VIEW memory_members AS
            SELECT * FROM (VALUES
                (DATE '2019-01-03','000001.SZ'),
                (DATE '2019-01-03','000002.SZ')) AS v(trade_date,ts_code)""")
        first = build_custom_research_universe(store, relation="memory_members", universe_id="memory_pool")
        repeat = build_custom_research_universe(store, relation="memory_members", universe_id="memory_pool")
        assert repeat["universe"]["sha256"] == first["universe"]["sha256"]
        configs = store.output_relation("l2", "custom_research_universe_config")
        config = store.rows(f"SELECT * FROM {configs} WHERE universe_id='memory_pool'")[0]
        assert config["source_format"] == "relation" and config["input_rows"] == 2
        assert json.loads(config["config_json"])["engine"] == "stage1_custom_relation_v1"
        assert validate_custom_research_universe(store, "memory_pool")["passed"]
        assert not (tmp_path / "members.parquet").exists()
        store.db.execute("""CREATE OR REPLACE TEMP VIEW memory_members AS
            SELECT DATE '2019-01-03' AS trade_date,'000002.SZ' AS ts_code""")
        with pytest.raises(ValueError, match="immutable"):
            build_custom_research_universe(store, relation="memory_members", universe_id="memory_pool")
        with pytest.raises(ValueError, match="exactly one"):
            build_custom_research_universe(store, "unused.csv", "bad_pool", relation="memory_members")


def test_member_file_rejects_duplicates_bad_codes_and_missing_states(tmp_path, golden_root):
    source = tmp_path / "members.csv"
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _dependencies(store)
        source.write_text("trade_date,ts_code\n20190103,000001.SZ\n20190103,000001.SZ\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Duplicate"):
            build_custom_research_universe(store, source, "duplicate_pool")
        source.write_text("trade_date,ts_code\n20190103,not-a-code\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid"):
            build_custom_research_universe(store, source, "bad_code_pool")
        source.write_text("trade_date,ts_code\n20190103,999999.SZ\n", encoding="utf-8")
        with pytest.raises(ValueError, match="historical name"):
            build_custom_research_universe(store, source, "missing_state_pool")
        with pytest.raises(ValueError, match="Invalid custom universe_id"):
            custom_universe_identity(store, "../escape")
