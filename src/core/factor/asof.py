"""Point-in-time financial as-of expansion for factor construction.

Stage 2 source directory: only L1 products are consumed through
Store.output_relation(); no raw-table access, no core.label import.

Two families of helpers, both producing one row per
(trade_date, ts_code, end_date) holding the version visible at that date:

metric_versions_daily   L1 quarterly_financial / ttm_financial for one metric.
                        Per end_date, versions become visible at available_date
                        and are superseded at the next version's available_date
                        (same-day supersession yields an empty interval for the
                        older version, so only the newest version_id survives).
balance_versions_daily  L1 financial_available restricted to balancesheet_vip
                        with availability_status='available', same interval
                        logic ordered by (available_date, effective_ann_date,
                        record_id).

current_metric_daily collapses a metric view to the LATEST visible end_date
per (trade_date, ts_code). If that end_date's visible version is not
status='available' the factor stays NULL -- there is deliberately NO fallback
to an older end_date (plan Step 5: ambiguous/missing stay empty).
"""
from core.data.store import qs
from core.market.prices import date_window


def _input_cutoff(input_end, column="available_date"):
    if input_end is None:
        return ""
    return f" AND {column}<=DATE {qs(input_end)}"


def metric_versions_daily(store, view_name, table, metric, input_end=None, start=None, end=None):
    if table not in ("quarterly_financial", "ttm_financial"):
        raise ValueError(f"Unsupported financial metric table: {table}")
    relation = store.output_relation("l1", table)
    calendar = store.output_relation("l1", "daily_calendar")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW {view_name} AS
        WITH versions AS (
            SELECT ts_code, end_date, available_date, value, status,
                   lead(available_date) OVER (
                       PARTITION BY ts_code, end_date
                       ORDER BY available_date, version_id) AS next_available_date
            FROM {relation}
            WHERE metric={qs(metric)}{_input_cutoff(input_end)}
        )
        SELECT c.trade_date, v.ts_code, v.end_date, v.value, v.status
        FROM versions v JOIN {calendar} c
          ON c.trade_date>=v.available_date
         AND (v.next_available_date IS NULL OR c.trade_date<v.next_available_date)
        WHERE {date_window(store, start, end, 'c.trade_date')}
    """)
    return view_name


def current_metric_daily(store, view_name, source_view):
    """Latest visible end_date per day/stock; non-available status keeps its row
    so callers can map it to NULL instead of silently using an older period."""
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW {view_name} AS
        SELECT trade_date, ts_code,
               max(end_date) AS end_date,
               arg_max(value, end_date) AS value,
               arg_max(status, end_date) AS status
        FROM {source_view} GROUP BY trade_date, ts_code
    """)
    return view_name


def balance_versions_daily(store, view_name, input_end=None, start=None, end=None):
    relation = store.output_relation("l1", "financial_available")
    calendar = store.output_relation("l1", "daily_calendar")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW {view_name} AS
        WITH versions AS (
            SELECT ts_code, end_date, available_date,
                   try_cast(total_assets AS DOUBLE) AS total_assets,
                   try_cast(total_hldr_eqy_exc_min_int AS DOUBLE) AS total_hldr_eqy_exc_min_int,
                   lead(available_date) OVER (
                       PARTITION BY ts_code, end_date
                       ORDER BY available_date, effective_ann_date, record_id) AS next_available_date
            FROM {relation}
            WHERE source_table='balancesheet_vip' AND availability_status='available'
                  {_input_cutoff(input_end)}
        )
        SELECT c.trade_date, v.ts_code, v.end_date, v.total_assets, v.total_hldr_eqy_exc_min_int
        FROM versions v JOIN {calendar} c
          ON c.trade_date>=v.available_date
         AND (v.next_available_date IS NULL OR c.trade_date<v.next_available_date)
        WHERE {date_window(store, start, end, 'c.trade_date')}
    """)
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW {view_name}_current AS
        SELECT trade_date, ts_code,
               max(end_date) AS end_date,
               arg_max(total_assets, end_date) AS total_assets,
               arg_max(total_hldr_eqy_exc_min_int, end_date) AS total_hldr_eqy_exc_min_int
        FROM {view_name} GROUP BY trade_date, ts_code
    """)
    return view_name
