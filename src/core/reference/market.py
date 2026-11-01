"""Raw ingestion for the official index daily-quote reference table.

index_daily sits outside the frozen Stage 1 nine-product scope
(configs/tushare_tables.yaml: stage1: false) but is a real provider table.
Same split as core.reference.membership: this module is the only place that
reads index_daily directly; core.benchmark consumes the validated L1 output
through Store.output_relation() only (enforced by
core.factor.entry.check_no_raw_access).
"""
from core.data.store import date_sql, qi


def build_index_daily_available(store) -> dict:
    """Validate and republish index_daily's official index OHLC quotes."""
    raw = store.raw("index_daily")
    key = store.rules["reference_market"]["index_daily_key"]
    if key != ["ts_code", "trade_date"]:
        raise ValueError("reference_market.index_daily_key must be [ts_code, trade_date]")
    numbers = ["open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"]
    normalized = f"""
        SELECT ts_code,
               {date_sql(qi('trade_date'))} AS trade_date,
               cast(trade_date AS VARCHAR) AS raw_trade_date,
               {', '.join(f'try_cast({qi(name)} AS DOUBLE) AS {qi(name)}' for name in numbers)},
               _source_file AS source_file, _source_row AS source_row
        FROM {raw}
    """
    store.db.execute(f"CREATE OR REPLACE TEMP VIEW normalized_index_daily AS {normalized}")
    invalid = ["trade_date IS NULL", "ts_code IS NULL", "trim(ts_code)=''",
               "close IS NULL", "pre_close IS NULL",
               "NOT isfinite(close)", "NOT isfinite(pre_close)",
               "close<=0", "pre_close<=0"]
    samples = store.rows("SELECT * FROM normalized_index_daily WHERE " + " OR ".join(invalid) + " LIMIT 10")
    if samples:
        raise ValueError(f"index_daily: invalid code/date/close/pre_close: {samples}")
    samples = store.rows("""SELECT ts_code, trade_date, count(*) AS duplicate_count
        FROM normalized_index_daily GROUP BY ts_code, trade_date HAVING count(*)>1 LIMIT 10""")
    if samples:
        raise ValueError(f"index_daily: duplicate (ts_code,trade_date) key: {samples}")
    query = """
        SELECT ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount,
               'index_daily' AS source_table, source_file, source_row
        FROM normalized_index_daily
    """
    return store.publish("l1", "index_daily_available", query, ["ts_code", "trade_date"])
