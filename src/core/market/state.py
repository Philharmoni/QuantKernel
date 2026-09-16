"""Historical listing facts; unsupported historical attributes stay unknown."""
from core.data.store import date_sql, qi
from core.market.prices import date_window


def build_stock_state(store, start=None, end=None) -> dict:
    raw = store.raw("stock_basic")
    calendar = store.output_relation("l1", "daily_calendar")
    normalized = f"""
        SELECT ts_code, curr_type,
               {date_sql(qi('list_date'))} AS list_date,
               {date_sql(qi('delist_date'))} AS delist_date,
               cast(list_date AS VARCHAR) AS raw_list_date,
               cast(delist_date AS VARCHAR) AS raw_delist_date,
               _source_file AS source_file, _source_row AS source_row
        FROM {raw}
    """
    store.db.execute(f"CREATE OR REPLACE TEMP VIEW normalized_stock_basic AS {normalized}")
    samples = store.rows("SELECT * FROM normalized_stock_basic WHERE list_date IS NULL "
                        "OR ts_code IS NULL OR NOT regexp_full_match(ts_code,'[^.[:space:]]+\\.(SH|SZ|BJ)') "
                        "OR (nullif(trim(raw_delist_date),'') IS NOT NULL AND delist_date IS NULL) "
                        "OR delist_date<list_date LIMIT 10")
    if samples:
        raise ValueError(f"stock_basic: invalid listing/delisting date or code: {samples}")
    samples = store.rows("SELECT ts_code,count(*) AS duplicate_count FROM normalized_stock_basic "
                        "GROUP BY ts_code HAVING count(*)>1 LIMIT 10")
    if samples:
        raise ValueError(f"stock_basic: duplicate ts_code: {samples}")
    query = f"""
        WITH indexed AS (
            SELECT b.*,
                   (SELECT min(trade_index) FROM {calendar} WHERE trade_date>=b.list_date) AS listing_index,
                   (SELECT max(trade_index) FROM {calendar} WHERE trade_date<b.delist_date) AS delisting_index
            FROM normalized_stock_basic b
        ), dated AS (
            SELECT c.trade_date, b.*, c.calendar_start,
                   c.trade_date>=b.list_date AND (b.delist_date IS NULL OR c.trade_date<b.delist_date) AS is_listed,
                   b.delist_date IS NOT NULL AND c.trade_date>=b.delist_date AS is_delisted,
                   c.trade_date<b.list_date AS is_not_yet_listed,
                   CASE WHEN c.trade_date<b.list_date OR b.delist_date<=c.calendar_start THEN 0
                        ELSE greatest(0, least(c.trade_index,coalesce(b.delisting_index,c.trade_index))
                             -coalesce(b.listing_index,c.trade_index+1)+1) END::BIGINT AS age_lower_bound
            FROM {calendar} c CROSS JOIN indexed b
            WHERE {date_window(store, start, end, 'c.trade_date')}
        )
        SELECT trade_date, ts_code, is_listed, is_delisted, is_not_yet_listed,
               curr_type='CNY' AS is_a_share,
               CASE WHEN is_not_yet_listed THEN 0 WHEN list_date<calendar_start THEN NULL
                    ELSE age_lower_bound END::BIGINT AS listing_trade_days,
               age_lower_bound AS listing_trade_days_lower_bound,
               CASE WHEN is_not_yet_listed THEN 'not_yet_listed'
                    WHEN list_date<calendar_start THEN 'left_censored' ELSE 'exact' END AS listing_age_status,
               CASE WHEN NOT is_listed THEN NULL WHEN ends_with(ts_code,'.SH') THEN 'SSE'
                    WHEN ends_with(ts_code,'.SZ') THEN 'SZSE' WHEN ends_with(ts_code,'.BJ') THEN 'BSE' END AS exchange,
               NULL::VARCHAR AS board,
               NULL::BOOLEAN AS is_st, NULL::BOOLEAN AS is_star_st, NULL::BOOLEAN AS is_delisting_period,
               'deferred_missing_history' AS historical_state_status,
               'stock_basic' AS source_table, source_file, source_row
        FROM dated
    """
    return store.publish("l1", "daily_stock_state", query, ["trade_date", "ts_code"])
