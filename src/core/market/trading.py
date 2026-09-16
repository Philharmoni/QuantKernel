"""Directional daily feasibility with explicit unknowns for unsupported facts."""
from core.calendar.trading import _date
from core.data.store import date_sql, qi, qs
from core.market.prices import date_window


def require_date_coverage(store, table: str, start=None, end=None):
    calendar = store.output_relation("l1", "daily_calendar")
    wanted = store.db.execute(f"SELECT min(trade_date),max(trade_date) FROM {calendar} "
                              f"WHERE {date_window(store,start,end)}").fetchone()
    relation = store.output_relation("l1", table)
    actual = store.db.execute(f"SELECT min(trade_date),max(trade_date) FROM {relation}").fetchone()
    if wanted[0] is not None and (actual[0] is None or actual[0]>wanted[0] or actual[1]<wanted[1]):
        raise ValueError(f"{table}: insufficient upstream date coverage; expected {wanted}, found {actual}")
    if table == "daily_stock_state":
        basic = store.raw("stock_basic")
        missing = store.rows(f"""SELECT c.trade_date,b.ts_code FROM {calendar} c CROSS JOIN
            (SELECT DISTINCT ts_code FROM {basic}) b ANTI JOIN {relation} s
            ON c.trade_date=s.trade_date AND b.ts_code=s.ts_code
            WHERE {date_window(store,start,end,'c.trade_date')} LIMIT 10""")
        if missing:
            raise ValueError(f"{table}: incomplete upstream coverage: {missing}")


def build_trade_status(store, start=None, end=None) -> dict:
    require_date_coverage(store, "daily_stock_state", start, end)
    require_date_coverage(store, "daily_adjusted_price", start, end)
    states = store.output_relation("l1", "daily_stock_state")
    prices = store.output_relation("l1", "daily_adjusted_price")
    suspend, limits = store.raw("suspend_d"), store.raw("limit_list_d")
    config = store.rules["trade_status"]
    for field in ("limit_events_complete", "suspension_events_complete"):
        if not isinstance(config[field], bool):
            raise ValueError(f"trade_status.{field} must be boolean")
    if config["intraday_suspension_policy"] != "conservative_block_with_explicit_reason":
        raise ValueError("Unsupported intraday_suspension_policy")
    suffixes = config["suspension_complete_code_suffixes"]
    if not isinstance(suffixes, list) or not suffixes or not set(suffixes) <= {'SH', 'SZ', 'BJ'}:
        raise ValueError("Invalid suspension_complete_code_suffixes")
    codes = config["limit_event_codes"]
    up, down, opened = (qs(codes[key]) for key in ("limit_up", "limit_down", "opened_limit_up"))
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW normalized_suspend AS
        SELECT ts_code, {date_sql(qi('trade_date'))} AS trade_date,
               upper(trim(suspend_type)) AS suspend_type,
               nullif(trim(cast(suspend_timing AS VARCHAR)),'') AS suspend_timing,
               _source_file, _source_row FROM {suspend}""")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW normalized_limits AS
        SELECT ts_code, {date_sql(qi('trade_date'))} AS trade_date,
               upper(trim({qi('limit')})) AS limit_type,
               _source_file, _source_row FROM {limits}""")
    for name, predicate in (("suspend_d", "suspend_type IS NULL OR suspend_type NOT IN ('S','R')"),
                            ("limit_list_d", f"limit_type IS NULL OR limit_type NOT IN ({up},{down},{opened})")):
        relation = "normalized_suspend" if name == "suspend_d" else "normalized_limits"
        samples = store.rows(f"SELECT * FROM {relation} WHERE trade_date IS NULL OR ts_code IS NULL "
                             f"OR trim(ts_code)='' OR {predicate} LIMIT 10")
        if samples:
            raise ValueError(f"{name}: invalid date/code/event type: {samples}")
    suspend_known = (f"{str(config['suspension_events_complete']).upper()} AND trade_date BETWEEN "
                     f"DATE {qs(_date(config['coverage_start']))} AND DATE {qs(_date(config['coverage_end']))}"
                     f" AND split_part(ts_code,'.',2) IN ({','.join(qs(s) for s in suffixes)})")
    limits_known = str(config["limit_events_complete"]).upper()
    # A complete-event declaration applies only to its declared source coverage.
    if config["limit_events_complete"]:
        limits_known += (f" AND trade_date BETWEEN DATE {qs(_date(config['limit_coverage_start']))} "
                         f"AND DATE {qs(_date(config['limit_coverage_end']))}")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW trade_facts AS
        WITH susp AS (
            SELECT trade_date,ts_code,
                   bool_or(suspend_type='S') AS observed_suspend,
                   bool_or(suspend_type='S' AND suspend_timing IS NULL) AS observed_full_day,
                   bool_or(suspend_type='S' AND suspend_timing IS NOT NULL) AS observed_intraday,
                   bool_or(suspend_type='R') AS observed_resume,
                   list(DISTINCT _source_file ORDER BY _source_file) AS source_suspend_files,
                   list(DISTINCT suspend_type ORDER BY suspend_type) AS observed_suspend_types
            FROM normalized_suspend GROUP BY trade_date,ts_code
        ), lim AS (
            SELECT trade_date,ts_code,
                   bool_or(limit_type={up}) AS observed_up,
                   bool_or(limit_type={down}) AS observed_down,
                   bool_or(limit_type={opened}) AS observed_opened,
                   list(DISTINCT limit_type ORDER BY limit_type) AS observed_limit_events,
                   list(DISTINCT _source_file ORDER BY _source_file) AS source_limit_files
            FROM normalized_limits GROUP BY trade_date,ts_code
        ), joined AS (
            SELECT st.trade_date,st.ts_code,st.is_listed,st.is_delisted,st.is_not_yet_listed,
                   p.ts_code IS NOT NULL AS quotation_exists,
                   p.close AS raw_close,p.vol,p.amount,
                   p.source_file AS source_price_file,p.source_row AS source_price_row,
                   st.source_file AS source_stock_file,
                   coalesce(s.observed_suspend,false) AS observed_suspend,
                   coalesce(s.observed_full_day,false) AS observed_full_day,
                   coalesce(s.observed_intraday,false) AS observed_intraday,
                   coalesce(s.observed_resume,false) AS has_resume_event,
                   coalesce(l.observed_up,false) AS observed_up,
                   coalesce(l.observed_down,false) AS observed_down,
                   coalesce(l.observed_opened,false) AS observed_opened,
                   coalesce(s.source_suspend_files,[]::VARCHAR[]) AS source_suspend_files,
                   coalesce(s.observed_suspend_types,[]::VARCHAR[]) AS observed_suspend_types,
                   coalesce(l.observed_limit_events,[]::VARCHAR[]) AS observed_limit_events,
                   coalesce(l.source_limit_files,[]::VARCHAR[]) AS source_limit_files
            FROM {states} st LEFT JOIN {prices} p USING(trade_date,ts_code)
            LEFT JOIN susp s USING(trade_date,ts_code) LEFT JOIN lim l USING(trade_date,ts_code)
            WHERE {date_window(store,start,end,'st.trade_date')}
        )
        SELECT *,
               CASE WHEN observed_suspend THEN true WHEN {suspend_known} THEN false ELSE NULL END AS is_suspended,
               CASE WHEN observed_full_day THEN true WHEN observed_suspend OR {suspend_known} THEN false ELSE NULL END AS is_full_day_suspended,
               CASE WHEN observed_intraday THEN true WHEN observed_suspend OR {suspend_known} THEN false ELSE NULL END AS is_intraday_suspended,
               CASE WHEN observed_up THEN true WHEN observed_down OR observed_opened OR {limits_known} THEN false ELSE NULL END AS is_limit_up,
               CASE WHEN observed_down THEN true WHEN observed_up OR {limits_known} THEN false ELSE NULL END AS is_limit_down
        FROM joined
    """)
    def reason(direction):
        flag, label = ("is_limit_up", "LIMIT_UP") if direction == "buy" else ("is_limit_down", "LIMIT_DOWN")
        return f"""CASE WHEN is_delisted THEN 'DELISTED' WHEN NOT is_listed THEN 'NOT_LISTED'
                    WHEN is_intraday_suspended THEN 'INTRADAY_SUSPENSION'
                    WHEN is_suspended THEN 'FULL_DAY_SUSPENSION'
                    WHEN NOT quotation_exists THEN 'NO_QUOTE' WHEN vol<=0 THEN 'NO_VOLUME'
                    WHEN {flag} THEN '{label}'
                    WHEN is_suspended IS NULL THEN 'SUSPENSION_STATUS_UNKNOWN'
                    WHEN {flag} IS NULL THEN 'LIMIT_STATUS_UNKNOWN' ELSE NULL END"""
    query = f"""WITH reasons AS (
        SELECT *,{reason('buy')} AS cannot_buy_reason,{reason('sell')} AS cannot_sell_reason FROM trade_facts
        ) SELECT *,
            CASE WHEN cannot_buy_reason IS NULL THEN true WHEN ends_with(cannot_buy_reason,'_UNKNOWN') THEN NULL ELSE false END AS can_buy,
            CASE WHEN cannot_sell_reason IS NULL THEN true WHEN ends_with(cannot_sell_reason,'_UNKNOWN') THEN NULL ELSE false END AS can_sell
        FROM reasons"""
    result = store.publish("l1", "daily_trade_status", query, ["trade_date", "ts_code"])
    diagnostics = {}
    for table in ("normalized_suspend", "normalized_limits"):
        missing = f"SELECT e.* FROM {table} e ANTI JOIN {states} st USING(trade_date,ts_code) WHERE {date_window(store,start,end,'e.trade_date')}"
        diagnostics[table] = {"unmatched_rows": store.db.execute(f"SELECT count(*) FROM ({missing})").fetchone()[0],
                              "samples": store.rows(missing + " LIMIT 10")}
    store.write_json(("l1", "daily_trade_status", "source_diagnostics.json"), diagnostics)
    return result
