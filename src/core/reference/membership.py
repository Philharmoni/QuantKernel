"""Raw ingestion for index/industry membership reference tables.

index_member_all and index_weight sit outside the frozen Stage 1 nine-product
scope (configs/tushare_tables.yaml: stage1: false) but are real provider
tables, not Stage 2 derived business logic. Mirroring core.financial's split
between raw ingestion/validation (there: available.py) and business
derivation (there: quarters.py), this module is the only place in the
codebase that reads index_member_all/index_weight directly; the Stage 2
business modules (core.industry.pit, core.benchmark.membership) consume its
L1 outputs exclusively through Store.output_relation(), never Store.raw()
(enforced by core.factor.entry.check_no_raw_access).
"""
from core.data.store import date_sql, qi


def build_index_member_available(store) -> dict:
    """Validate and republish index_member_all's SW-style industry assignments."""
    raw = store.raw("index_member_all")
    key = store.rules["reference_membership"]["index_member_key"]
    if key != ["ts_code", "l1_code", "in_date"]:
        raise ValueError("reference_membership.index_member_key must be [ts_code, l1_code, in_date]")
    normalized = f"""
        SELECT ts_code, l1_code, l1_name, l2_code, l2_name, l3_code, l3_name,
               {date_sql(qi('in_date'))} AS in_date,
               {date_sql(qi('out_date'))} AS out_date,
               cast(in_date AS VARCHAR) AS raw_in_date,
               cast(out_date AS VARCHAR) AS raw_out_date,
               nullif(trim(cast(is_new AS VARCHAR)), '') AS is_new,
               _source_file AS source_file, _source_row AS source_row
        FROM {raw}
    """
    store.db.execute(f"CREATE OR REPLACE TEMP VIEW normalized_index_member AS {normalized}")
    samples = store.rows("""SELECT * FROM normalized_index_member
        WHERE ts_code IS NULL OR NOT regexp_full_match(ts_code,'[^.[:space:]]+\\.(SH|SZ|BJ)')
           OR l1_code IS NULL OR trim(l1_code)=''
           OR in_date IS NULL
           OR (nullif(trim(raw_out_date),'') IS NOT NULL AND out_date IS NULL)
           OR (out_date IS NOT NULL AND out_date<in_date)
           OR (is_new IS NOT NULL AND is_new NOT IN ('Y','N')) LIMIT 10""")
    if samples:
        raise ValueError(f"index_member_all: invalid code/date/is_new: {samples}")
    samples = store.rows("""SELECT ts_code, l1_code, in_date, count(*) AS duplicate_count
        FROM normalized_index_member GROUP BY ts_code, l1_code, in_date HAVING count(*)>1 LIMIT 10""")
    if samples:
        raise ValueError(f"index_member_all: duplicate (ts_code,l1_code,in_date) key: {samples}")
    query = """
        SELECT ts_code, l1_code, l1_name, l2_code, l2_name, l3_code, l3_name,
               in_date, out_date, is_new,
               'index_member_all' AS source_table, source_file, source_row
        FROM normalized_index_member
    """
    return store.publish("l1", "index_member_available", query, ["ts_code", "l1_code", "in_date"])


def build_index_weight_available(store) -> dict:
    """Validate and republish index_weight's periodic constituent weights."""
    raw = store.raw("index_weight")
    key = store.rules["reference_membership"]["index_weight_key"]
    if key != ["index_code", "con_code", "trade_date"]:
        raise ValueError("reference_membership.index_weight_key must be [index_code, con_code, trade_date]")
    normalized = f"""
        SELECT index_code, con_code,
               {date_sql(qi('trade_date'))} AS trade_date,
               cast(trade_date AS VARCHAR) AS raw_trade_date,
               cast(weight AS DOUBLE) AS weight,
               _source_file AS source_file, _source_row AS source_row
        FROM {raw}
    """
    store.db.execute(f"CREATE OR REPLACE TEMP VIEW normalized_index_weight AS {normalized}")
    samples = store.rows("""SELECT * FROM normalized_index_weight
        WHERE index_code IS NULL OR trim(index_code)=''
           OR con_code IS NULL OR NOT regexp_full_match(con_code,'[^.[:space:]]+\\.(SH|SZ|BJ)')
           OR trade_date IS NULL
           OR weight IS NULL OR weight<0 OR NOT isfinite(weight) LIMIT 10""")
    if samples:
        raise ValueError(f"index_weight: invalid code/date/weight: {samples}")
    samples = store.rows("""SELECT index_code, con_code, trade_date, count(*) AS duplicate_count
        FROM normalized_index_weight GROUP BY index_code, con_code, trade_date HAVING count(*)>1 LIMIT 10""")
    if samples:
        raise ValueError(f"index_weight: duplicate (index_code,con_code,trade_date) key: {samples}")
    query = """
        SELECT index_code, con_code, trade_date, weight,
               'index_weight' AS source_table, source_file, source_row
        FROM normalized_index_weight
    """
    return store.publish("l1", "index_weight_available", query, ["index_code", "con_code", "trade_date"])
