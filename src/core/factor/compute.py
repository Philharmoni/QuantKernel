"""Reusable PIT inputs and generic computations for per-factor source files."""
from core.data.store import date_sql, qs
from core.factor.asof import balance_versions_daily, current_metric_daily, metric_versions_daily
from core.market.prices import date_window


def prepare_price_input(store, input_end=None):
    marker = ("_factor_plugin_price_cutoff", input_end)
    if getattr(store, "_factor_plugin_input_marker", None) == marker:
        return "factor_price_input"
    prices = store.output_relation("l1", "daily_adjusted_price")
    calendar = store.output_relation("l1", "daily_calendar")
    cutoff = f"WHERE p.trade_date<={date_sql(qs(input_end))}" if input_end else ""
    store.db.execute(f"""CREATE OR REPLACE TEMP TABLE factor_price_input AS
        SELECT p.trade_date, p.ts_code, c.trade_index,
               p.adjusted_close, p.ret_1d,
               try_cast(p.total_mv AS DOUBLE) AS total_mv,
               try_cast(p.circ_mv AS DOUBLE) AS circ_mv,
               try_cast(p.turnover_rate AS DOUBLE) AS turnover_rate,
               p.amount
        FROM {prices} p JOIN {calendar} c USING(trade_date)
        {cutoff}
    """)
    store._factor_plugin_input_marker = marker
    store._factor_plugin_windows_ready = False
    return "factor_price_input"


def prepare_price_windows(store, input_end=None):
    prepare_price_input(store, input_end)
    if getattr(store, "_factor_plugin_windows_ready", False):
        return "factor_price_windows"
    store.db.execute("""CREATE OR REPLACE TEMP VIEW factor_price_windows AS
        SELECT *,
               count(ret_1d) OVER w20 AS ret_n20,
               sum(cast(ret_1d AS DECIMAL(38,24))) OVER w20 AS ret_s1_20,
               sum(cast(ret_1d*ret_1d AS DECIMAL(38,24))) OVER w20 AS ret_s2_20,
               count(ret_1d) OVER w60 AS ret_n60,
               sum(cast(ret_1d AS DECIMAL(38,24))) OVER w60 AS ret_s1_60,
               sum(cast(ret_1d*ret_1d AS DECIMAL(38,24))) OVER w60 AS ret_s2_60,
               count(turnover_rate) OVER w20 AS turn_n20,
               sum(cast(turnover_rate AS DECIMAL(38,18))) OVER w20 AS turn_s20,
               count(CASE WHEN ret_1d IS NOT NULL AND amount>0 THEN 1 END) OVER w20 AS amihud_n20,
               sum(cast(CASE WHEN amount>0 THEN abs(ret_1d)/(amount*1000) END
                        AS DECIMAL(38,24))) OVER w20 AS amihud_s20,
               count(amount) OVER w20 AS amount_n20,
               sum(cast(amount*1000 AS DECIMAL(38,18))) OVER w20 AS amount_s20
        FROM factor_price_input
        WINDOW w20 AS (PARTITION BY ts_code ORDER BY trade_index
                       RANGE BETWEEN 19 PRECEDING AND CURRENT ROW),
               w60 AS (PARTITION BY ts_code ORDER BY trade_index
                       RANGE BETWEEN 59 PRECEDING AND CURRENT ROW)
    """)
    store._factor_plugin_windows_ready = True
    return "factor_price_windows"


def _result(store, factor_id, query):
    name = "factor_plugin_" + factor_id
    store.db.execute(f"CREATE OR REPLACE TEMP VIEW {name} AS {query}")
    return name


def simple_price_factor(store, factor_id, expression, condition, start=None, end=None, input_end=None):
    prepare_price_input(store, input_end)
    window = date_window(store, start, end, "trade_date")
    return _result(store, factor_id, f"""
        SELECT trade_date, ts_code, '{factor_id}' AS factor_id,
               cast({expression} AS DOUBLE) AS value
        FROM factor_price_input
        WHERE {condition} AND isfinite({expression}) AND {window}
    """)


def rolling_price_factor(store, factor_id, expression, condition, start=None, end=None, input_end=None):
    prepare_price_windows(store, input_end)
    window = date_window(store, start, end, "trade_date")
    return _result(store, factor_id, f"""
        SELECT trade_date, ts_code, '{factor_id}' AS factor_id,
               cast({expression} AS DOUBLE) AS value
        FROM factor_price_windows
        WHERE {condition} AND isfinite({expression}) AND {window}
    """)


def momentum_factor(store, factor_id, lookback, skip=0, start=None, end=None, input_end=None):
    prepare_price_input(store, input_end)
    window = date_window(store, start, end, "cur.trade_date")
    numerator = "cur" if not skip else "skipped"
    skip_join = "" if not skip else f"""LEFT JOIN factor_price_input skipped
        ON skipped.ts_code=cur.ts_code AND skipped.trade_index=cur.trade_index-{int(skip)}"""
    return _result(store, factor_id, f"""
        SELECT cur.trade_date, cur.ts_code, '{factor_id}' AS factor_id,
               {numerator}.adjusted_close/base.adjusted_close-1 AS value
        FROM factor_price_input cur
        {skip_join}
        LEFT JOIN factor_price_input base
          ON base.ts_code=cur.ts_code AND base.trade_index=cur.trade_index-{int(lookback)}
        WHERE {numerator}.adjusted_close/base.adjusted_close-1 IS NOT NULL
          AND isfinite({numerator}.adjusted_close/base.adjusted_close-1) AND {window}
    """)


def _ttm_current(store, metric, input_end, start, end):
    view = f"factor_plugin_ttm_{metric}"
    metric_versions_daily(store, view + "_versions", "ttm_financial", metric, input_end, start, end)
    current_metric_daily(store, view, view + "_versions")
    return view


def ttm_market_cap_factor(store, factor_id, metric, start=None, end=None, input_end=None):
    prepare_price_input(store, input_end)
    current = _ttm_current(store, metric, input_end, start, end)
    return _result(store, factor_id, f"""
        SELECT m.trade_date, m.ts_code, '{factor_id}' AS factor_id,
               m.value/(p.total_mv*10000) AS value
        FROM {current} m JOIN factor_price_input p
          ON p.trade_date=m.trade_date AND p.ts_code=m.ts_code
        WHERE m.status='available' AND m.value IS NOT NULL
          AND isfinite(m.value/(p.total_mv*10000)) AND p.total_mv>0
    """)


def balance_market_cap_factor(store, factor_id, field, start=None, end=None, input_end=None):
    prepare_price_input(store, input_end)
    balance_versions_daily(store, "factor_plugin_balance", input_end, start, end)
    return _result(store, factor_id, f"""
        SELECT b.trade_date, b.ts_code, '{factor_id}' AS factor_id,
               b.{field}/(p.total_mv*10000) AS value
        FROM factor_plugin_balance_current b JOIN factor_price_input p
          ON p.trade_date=b.trade_date AND p.ts_code=b.ts_code
        WHERE b.{field} IS NOT NULL AND p.total_mv>0
          AND isfinite(b.{field}/(p.total_mv*10000))
    """)


def quarterly_yoy_factor(store, factor_id, metric, start=None, end=None, input_end=None):
    view = f"factor_plugin_quarterly_{metric}"
    metric_versions_daily(store, view + "_versions", "quarterly_financial", metric, input_end, start, end)
    current_metric_daily(store, view, view + "_versions")
    return _result(store, factor_id, f"""
        SELECT cur.trade_date, cur.ts_code, '{factor_id}' AS factor_id,
               cur.value/prev.value-1 AS value
        FROM {view} cur
        LEFT JOIN {view}_versions prev
          ON prev.trade_date=cur.trade_date AND prev.ts_code=cur.ts_code
         AND prev.end_date=cur.end_date-INTERVAL 1 YEAR
        WHERE cur.status='available' AND prev.status='available'
          AND cur.value IS NOT NULL AND prev.value IS NOT NULL AND prev.value<>0
          AND isfinite(cur.value/prev.value-1)
    """)


def ttm_balance_factor(store, factor_id, field, start=None, end=None, input_end=None):
    balance_versions_daily(store, "factor_plugin_balance", input_end, start, end)
    current = _ttm_current(store, "n_income_attr_p", input_end, start, end)
    return _result(store, factor_id, f"""
        SELECT m.trade_date, m.ts_code, '{factor_id}' AS factor_id,
               m.value/b.{field} AS value
        FROM {current} m JOIN factor_plugin_balance_current b
          ON b.trade_date=m.trade_date AND b.ts_code=m.ts_code
        WHERE m.status='available' AND m.value IS NOT NULL
          AND b.{field} IS NOT NULL AND b.{field}<>0
          AND isfinite(m.value/b.{field})
    """)
