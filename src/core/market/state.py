"""Historical identity state with namechange-derived ST status and explicit unknowns."""
from core.data.store import date_sql, qi, qs
from core.market.prices import date_window


def _validate_namechange(store, rules):
    """Normalize and gate the raw namechange intervals before any derivation."""
    if rules.get("name_source") != "namechange":
        raise ValueError("historical_status.name_source must be namechange")
    if rules.get("st_name_rule") != "name_contains_st":
        raise ValueError("historical_status.st_name_rule must be name_contains_st")
    if rules.get("star_st_name_rule") != "name_contains_star_st":
        raise ValueError("historical_status.star_st_name_rule must be name_contains_star_st")
    if rules.get("interval_semantics") != "closed_start_closed_end":
        raise ValueError("historical_status.interval_semantics must be closed_start_closed_end")
    if rules.get("overlap_policy") != "agreed_value_else_null_ambiguous":
        raise ValueError("historical_status.overlap_policy must be agreed_value_else_null_ambiguous")
    if rules.get("uncovered_policy") != "null_with_uncovered_status":
        raise ValueError("historical_status.uncovered_policy must be null_with_uncovered_status")
    raw = store.raw("namechange")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW normalized_namechange AS
        SELECT ts_code,
               nullif(trim(cast({qi('name')} AS VARCHAR)), '') AS name,
               upper(nullif(trim(cast({qi('name')} AS VARCHAR)), '')) AS upper_name,
               {date_sql(qi('start_date'))} AS start_date,
               {date_sql(qi('end_date'))} AS end_date,
               cast({qi('start_date')} AS VARCHAR) AS raw_start_date,
               cast({qi('end_date')} AS VARCHAR) AS raw_end_date,
               _source_file AS name_source_file, _source_row AS name_source_row
        FROM {raw}
    """)
    samples = store.rows("""SELECT ts_code, raw_start_date, raw_end_date, name, name_source_file, name_source_row
        FROM normalized_namechange
        WHERE ts_code IS NULL OR trim(cast(ts_code AS VARCHAR))=''
           OR NOT regexp_full_match(ts_code,'[^.[:space:]]+\\.(SH|SZ|BJ)')
           OR name IS NULL
           OR start_date IS NULL
           OR (nullif(trim(cast(raw_end_date AS VARCHAR)),'') IS NOT NULL AND end_date IS NULL)
           OR (end_date IS NOT NULL AND end_date<start_date) LIMIT 10""")
    if samples:
        raise ValueError(f"namechange: invalid code/name/date interval: {samples}")
    samples = store.rows("""SELECT ts_code, start_date, count(*) AS duplicate_count
        FROM normalized_namechange GROUP BY ts_code, start_date HAVING count(*)>1 LIMIT 10""")
    if samples:
        raise ValueError(f"namechange: duplicate (ts_code,start_date) key: {samples}")


def build_stock_state(store, start=None, end=None) -> dict:
    raw = store.raw("stock_basic")
    calendar = store.output_relation("l1", "daily_calendar")
    rules = store.rules["historical_status"]
    _validate_namechange(store, rules)
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
    # Per security-day facts from every covering namechange interval. Overlapping
    # intervals are preserved; a derived flag is only published when all covering
    # names agree, otherwise it stays unknown with an explicit status.
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW namechange_facts AS
        WITH covering AS (
            SELECT c.trade_date, n.ts_code, n.name, n.upper_name, n.start_date,
                   n.name_source_file, n.name_source_row
            FROM {calendar} c JOIN normalized_namechange n
              ON c.trade_date>=n.start_date
             AND c.trade_date<=coalesce(n.end_date, DATE '9999-12-31')
        ), aggregated AS (
            SELECT trade_date, ts_code,
                   count(*) AS covering_intervals,
                   bool_or(contains(upper_name,'ST')) AS any_st,
                   bool_and(contains(upper_name,'ST')) AS all_st,
                   bool_or(contains(upper_name,'*ST')) AS any_star_st,
                   bool_and(contains(upper_name,'*ST')) AS all_star_st
            FROM covering GROUP BY trade_date, ts_code
        ), picked AS (
            SELECT trade_date, ts_code, name AS historical_name,
                   name_source_file, name_source_row
            FROM (SELECT trade_date, ts_code, name, name_source_file, name_source_row,
                         row_number() OVER (PARTITION BY trade_date, ts_code
                             ORDER BY start_date DESC, name_source_file, name_source_row) AS rn
                  FROM covering) ranked WHERE rn=1
        )
        SELECT a.trade_date, a.ts_code, a.covering_intervals, a.any_st, a.all_st,
               a.any_star_st, a.all_star_st, p.historical_name,
               p.name_source_file, p.name_source_row
        FROM aggregated a LEFT JOIN picked p USING (trade_date, ts_code)
    """)
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
        ), enriched AS (
            SELECT d.*, f.covering_intervals, f.any_st, f.all_st, f.any_star_st, f.all_star_st,
                   f.historical_name, f.name_source_file, f.name_source_row
            FROM dated d LEFT JOIN namechange_facts f USING (trade_date, ts_code)
        )
        SELECT trade_date, ts_code, is_listed, is_delisted, is_not_yet_listed,
               curr_type='CNY' AS is_a_share,
               CASE WHEN is_not_yet_listed THEN 0 WHEN list_date<calendar_start THEN NULL
                    ELSE age_lower_bound END::BIGINT AS listing_trade_days,
               age_lower_bound AS listing_trade_days_lower_bound,
               CASE WHEN is_not_yet_listed THEN 'not_yet_listed'
                    WHEN list_date<calendar_start THEN 'left_censored' ELSE 'exact' END AS listing_age_status,
               CASE WHEN is_not_yet_listed THEN 0
                    WHEN list_date IS NULL THEN NULL
                    ELSE greatest(0, date_diff('day', list_date,
                         CASE WHEN is_delisted THEN delist_date - INTERVAL 1 DAY ELSE trade_date END)) END::BIGINT
                    AS listing_natural_days,
               CASE WHEN is_not_yet_listed THEN 0
                    WHEN list_date IS NULL THEN NULL
                    ELSE round(greatest(0, date_diff('day', list_date,
                         CASE WHEN is_delisted THEN delist_date - INTERVAL 1 DAY ELSE trade_date END))/365.25*252) END::BIGINT
                    AS estimated_listing_trade_days,
               CASE WHEN NOT is_listed THEN NULL WHEN ends_with(ts_code,'.SH') THEN 'SSE'
                    WHEN ends_with(ts_code,'.SZ') THEN 'SZSE' WHEN ends_with(ts_code,'.BJ') THEN 'BSE' END AS exchange,
               NULL::VARCHAR AS board,
               CASE WHEN NOT is_listed THEN NULL
                    WHEN covering_intervals IS NULL OR covering_intervals=0 THEN NULL
                    WHEN any_st<>all_st THEN NULL ELSE any_st END AS is_st,
               CASE WHEN NOT is_listed THEN NULL
                    WHEN covering_intervals IS NULL OR covering_intervals=0 THEN NULL
                    WHEN any_star_st<>all_star_st THEN NULL ELSE any_star_st END AS is_star_st,
               NULL::BOOLEAN AS is_delisting_period,
               CASE WHEN NOT is_listed THEN 'not_listed'
                    WHEN covering_intervals IS NULL OR covering_intervals=0 THEN 'namechange_uncovered'
                    WHEN any_st<>all_st OR any_star_st<>all_star_st THEN 'namechange_ambiguous'
                    ELSE 'namechange_derived' END AS historical_state_status,
               CASE WHEN NOT is_listed THEN NULL ELSE historical_name END AS historical_name,
               CASE WHEN NOT is_listed THEN NULL ELSE name_source_file END AS name_source_file,
               CASE WHEN NOT is_listed THEN NULL ELSE name_source_row END AS name_source_row,
               'stock_basic' AS source_table, source_file, source_row
        FROM enriched
    """
    result = store.publish("l1", "daily_stock_state", query, ["trade_date", "ts_code"])
    diagnostics = {}
    missing = f"""SELECT n.ts_code, min(n.start_date) AS first_start, max(coalesce(n.end_date, DATE '9999-12-31')) AS last_end
        FROM normalized_namechange n ANTI JOIN normalized_stock_basic b USING(ts_code) GROUP BY n.ts_code"""
    diagnostics["namechange_unmatched_codes"] = {
        "count": store.db.execute(f"SELECT count(*) FROM ({missing})").fetchone()[0],
        "samples": store.rows(missing + " ORDER BY ts_code LIMIT 10")}
    coverage = """SELECT
        count(*) FILTER (WHERE nc.codes IS NULL) AS stocks_without_namechange,
        count(*) FILTER (WHERE nc.first_start IS NOT NULL AND nc.first_start>b.list_date) AS stocks_with_late_first_interval
        FROM normalized_stock_basic b LEFT JOIN (
            SELECT ts_code, count(*) AS codes, min(start_date) AS first_start FROM normalized_namechange GROUP BY ts_code) nc
        USING(ts_code)"""
    diagnostics["namechange_coverage"] = store.rows(coverage)[0]
    store.write_json(("l1", "daily_stock_state", "source_diagnostics.json"), diagnostics)
    return result
