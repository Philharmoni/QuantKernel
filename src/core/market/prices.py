from core.calendar.trading import TradingCalendar
from core.data.store import date_sql, qi, qs


def date_window(store, start=None, end=None, column="trade_date") -> str:
    calendar = TradingCalendar.from_store(store)
    lower = calendar.calendar_start if start is None else calendar._bounded(start)
    upper = calendar.calendar_end if end is None else calendar._bounded(end)
    if lower > upper:
        raise ValueError(f"Start date {lower} must not be after end date {upper}")
    return f"{column} BETWEEN DATE {qs(lower)} AND DATE {qs(upper)}"


def build_adjusted_price(store, start=None, end=None) -> dict:
    if store.rules["price_adjustment"] != "raw_times_adj_factor":
        raise ValueError("Unsupported price_adjustment; V1 uses raw_times_adj_factor")
    raw = store.raw("stk_factor_pro")
    calendar = store.output_relation("l1", "daily_calendar")
    numbers = ["open", "high", "low", "close", "adj_factor", "vol", "amount"]
    excluded = ["trade_date", "_source_file", "_source_row", *numbers]
    normalized = f"""
        SELECT * EXCLUDE ({', '.join(qi(name) for name in excluded)}),
               {date_sql(qi('trade_date'))} AS trade_date,
               cast(trade_date AS VARCHAR) AS raw_trade_date,
               {', '.join(f'try_cast({qi(name)} AS DOUBLE) AS {qi(name)}' for name in numbers)},
               _source_file AS source_file, _source_row AS source_row,
               'stk_factor_pro' AS source_table
        FROM {raw}
    """
    store.db.execute(f"CREATE OR REPLACE TEMP VIEW normalized_prices AS {normalized}")
    invalid = ["trade_date IS NULL", "ts_code IS NULL", "trim(ts_code)=''"]
    for name in numbers:
        invalid.extend([f"{qi(name)} IS NULL", f"NOT isfinite({qi(name)})",
                        f"{qi(name)} {'<=' if name not in ('vol', 'amount') else '<'} 0"])
    invalid.extend(["low > high", "open < low", "open > high", "close < low", "close > high"])
    invalid.extend(f"NOT isfinite({qi(name)} * adj_factor)" for name in ("open", "high", "low", "close"))
    samples = store.rows("SELECT ts_code, trade_date, raw_trade_date, open, high, low, close, adj_factor, source_file, source_row "
                         "FROM normalized_prices WHERE " + " OR ".join(invalid) + " LIMIT 10")
    if samples:
        raise ValueError(f"stk_factor_pro: invalid price/date/adj_factor/volume: {samples}")
    samples = store.rows("SELECT trade_date, ts_code, count(*) AS duplicate_count FROM normalized_prices "
                         "GROUP BY trade_date, ts_code HAVING count(*)>1 ORDER BY trade_date, ts_code LIMIT 10")
    if samples:
        raise ValueError(f"stk_factor_pro: duplicate price key before return calculation: {samples}")
    samples = store.rows(f"SELECT p.ts_code,p.trade_date,p.source_file FROM normalized_prices p "
                         f"ANTI JOIN {calendar} c ON p.trade_date=c.trade_date LIMIT 10")
    if samples:
        raise ValueError(f"stk_factor_pro: quote dates absent from daily_calendar: {samples}")
    adjusted = ", ".join(f"{qi(name)} * adj_factor AS adjusted_{name}" for name in ("open", "high", "low", "close"))
    query = f"""
        WITH adjusted AS (
            SELECT *, {adjusted} FROM normalized_prices
        ), previous AS (
            SELECT *, lag(trade_date) OVER w AS prev_quote_date,
                      lag(adjusted_close) OVER w AS prev_adjusted_close
            FROM adjusted
            WINDOW w AS (PARTITION BY ts_code ORDER BY trade_date)
        ), returns AS (
            SELECT p.* EXCLUDE (prev_adjusted_close),
                   CASE WHEN p.prev_quote_date=c.prev_trade_date
                        THEN p.adjusted_close/p.prev_adjusted_close-1 ELSE NULL END AS ret_1d,
                   CASE WHEN p.prev_quote_date IS NULL THEN 'no_previous_quote'
                        WHEN p.prev_quote_date<>c.prev_trade_date THEN 'missing_previous_trading_day'
                        ELSE 'available' END AS return_status
            FROM previous p JOIN {calendar} c USING (trade_date)
        )
        SELECT * FROM returns WHERE {date_window(store, start, end)}
    """
    return store.publish("l1", "daily_adjusted_price", query, ["trade_date", "ts_code"])
