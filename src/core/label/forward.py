"""Forward-return labels with explicit exception paths (plan sections 4.1/4.2).

Stage 2 source directory: this module must never call Store's raw-table
reader (enforced by core.factor.entry.check_no_raw_access); it only consumes
L1 products. It must never be imported by core.factor (enforced by
core.factor.entry.check_factor_label_isolation).

Two label families are emitted into one long-format L3 table keyed by
(trade_date, ts_code, label_id):

  ret_o2o_h   primary: enter at T+1 open, exit at T+1+h open.
  ret_c2c_h   reference only: T close to T+h close (a theoretical upper bound,
              never the primary evidence; no entry_open_at_up_limit concept
              applies because its entry is a close, not an open).

All prices are Stage 1 adjusted prices (raw * same-day adj_factor); vendor
qfq/hfq series are never used. Delisting and delay-cap exits use the last
available quotation's adjusted_close, i.e. the final observable price, since
no later open exists to trade at.

Exception flags (configs/factor_research.yaml labels.exception_flags), in the
priority order used for the single exception_flag column:

  beyond_data_end        the planned exit trading date does not exist within
                         calendar coverage (also when T+1 itself does not
                         exist); the label is NULL rather than a partial
                         window. SA006-style already-delisted stocks are not
                         in scope here: only stocks listed at T get a row.
  entry_blocked          no quotation at entry, or not listed at entry; the
                         label is NULL.
  delisted_in_window     the planned exit has no quotation because the stock
                         is delisted there; exit at the last quote's
                         adjusted_close, sample retained (never dropped).
  exit_delayed_days      the planned exit has no quotation but the stock is
                         not delisted: exit at the next quotation's open
                         (o2o) / close (c2c) within
                         labels.max_exit_delay_trading_days; if no quotation
                         resumes within that cap, exit at the last quote's
                         adjusted_close and exit_delayed_days records the cap.
  entry_open_at_up_limit raw open at entry >= up_limit at entry (exact DOUBLE
                         comparison, same convention as
                         core.market.trading's price_limit_up); the label is
                         still computed, the flag only marks the condition.

Because exception_flag is a single priority-ordered summary, the independent
boolean columns entry_blocked / entry_open_at_up_limit / delisted_in_window /
beyond_data_end are also emitted so downstream "tradable" filters never lose
the entry condition when an exit anomaly coexists.
"""
from core.config.settings import load_settings
from core.market.prices import date_window

_LABEL_FAMILY = {"o2o": "open", "c2c": "close"}


def _lead_specs(horizon: int) -> dict:
    """offset -> {column: alias suffix} needed for one horizon's two labels."""
    specs = {}

    def need(offset, columns):
        specs.setdefault(offset, [])
        for column in columns:
            if column not in specs[offset]:
                specs[offset].append(column)

    # o2o entry at T+1 (raw open + up_limit drive entry_open_at_up_limit).
    need(1, ["trade_date", "quotation_exists", "adjusted_open", "open", "up_limit", "is_listed"])
    # o2o planned exit at T+1+h, c2c planned exit at T+h.
    for family, offset in (("o2o", 1 + horizon), ("c2c", horizon)):
        kind = _LABEL_FAMILY[family]
        need(offset, ["trade_date", "quotation_exists", f"adjusted_{kind}", "is_delisted",
                      "nq_index", f"nq_{kind}", "nq_date", "lq_close", "lq_date"])
    return specs


def _exit_expressions(prefix: str, offset: int, kind: str, max_delay: int) -> dict:
    quote = f"{prefix}_quotation_exists"
    delisted = f"{prefix}_is_delisted"
    planned_index = f"(trade_index+{offset})"
    delayed = (f"{prefix}_nq_index IS NOT NULL AND "
               f"{prefix}_nq_index-{planned_index}<={max_delay}")
    return {
        "planned_index": planned_index,
        "delayed": delayed,
        "exit_date": (f"CASE WHEN {quote} THEN {prefix}_trade_date "
                      f"WHEN {delisted} THEN {prefix}_lq_date "
                      f"WHEN {delayed} THEN {prefix}_nq_date "
                      f"ELSE {prefix}_lq_date END"),
        "exit_price": (f"CASE WHEN {quote} THEN {prefix}_adjusted_{kind} "
                       f"WHEN {delisted} THEN {prefix}_lq_close "
                       f"WHEN {delayed} THEN {prefix}_nq_{kind} "
                       f"ELSE {prefix}_lq_close END"),
        "exit_delayed_days": (f"CASE WHEN {quote} THEN 0 WHEN {delisted} THEN NULL "
                              f"WHEN {delayed} THEN {prefix}_nq_index-{planned_index} "
                              f"ELSE {max_delay} END"),
        "delisted_flag": f"NOT {quote} AND {delisted}",
        "delayed_flag": f"NOT {quote} AND NOT {delisted}",
    }


def _label_view(family: str, horizon: int, max_delay: int, window: str) -> str:
    kind = _LABEL_FAMILY[family]
    label_id = f"ret_{family}_{horizon}"
    leads = []
    for offset, columns in sorted(_lead_specs(horizon).items()):
        for column in columns:
            leads.append(f"lead({column},{offset}) OVER w AS o{offset}_{column}")
    lead_sql = ",\n                   ".join(leads)
    if family == "o2o":
        entry_offset, exit_offset = 1, 1 + horizon
        entry_prefix = f"o{entry_offset}"
        entry = {
            "date": f"{entry_prefix}_trade_date",
            "price": f"{entry_prefix}_adjusted_open",
            "blocked": f"NOT {entry_prefix}_quotation_exists OR NOT {entry_prefix}_is_listed",
            "up_limit": (f"{entry_prefix}_open IS NOT NULL AND {entry_prefix}_up_limit IS NOT NULL "
                         f"AND {entry_prefix}_open>={entry_prefix}_up_limit"),
        }
    else:
        exit_offset = horizon
        entry = {"date": "trade_date", "price": "adjusted_close",
                 "blocked": "NOT quotation_exists", "up_limit": "false"}
    exit_prefix = f"o{exit_offset}"
    ex = _exit_expressions(exit_prefix, exit_offset, kind, max_delay)
    beyond = f"{entry['date']} IS NULL OR {exit_prefix}_trade_date IS NULL"
    blocked = f"NOT ({beyond}) AND ({entry['blocked']})"
    return f"""
        WITH sequenced AS (
            SELECT *, {lead_sql}
            FROM label_panel
            WINDOW w AS (PARTITION BY ts_code ORDER BY trade_index)
        ), flags AS (
            SELECT *,
                   {beyond} AS beyond_data_end,
                   {blocked} AS entry_blocked
            FROM sequenced
            WHERE is_listed AND {window}
        ), resolved AS (
            SELECT *,
                   CASE WHEN beyond_data_end OR entry_blocked THEN NULL
                        ELSE {entry['price']} END AS entry_price,
                   CASE WHEN beyond_data_end OR entry_blocked THEN NULL
                        ELSE {ex['exit_date']} END AS exit_date,
                   CASE WHEN beyond_data_end OR entry_blocked THEN NULL
                        ELSE {ex['exit_price']} END AS exit_price,
                   CASE WHEN beyond_data_end OR entry_blocked THEN NULL
                        ELSE {ex['exit_delayed_days']} END AS exit_delayed_days,
                   NOT beyond_data_end AND NOT entry_blocked AND ({ex['delisted_flag']}) AS delisted_in_window,
                   NOT beyond_data_end AND NOT entry_blocked AND ({ex['delayed_flag']}) AS exit_delayed,
                   NOT beyond_data_end AND NOT entry_blocked AND ({entry['up_limit']}) AS entry_open_at_up_limit
            FROM flags
        )
        SELECT trade_date, ts_code, '{label_id}' AS label_id, {horizon} AS horizon,
               {entry['date']} AS entry_date,
               {exit_prefix}_trade_date AS planned_exit_date,
               exit_date, entry_price, exit_price,
               CASE WHEN beyond_data_end OR entry_blocked THEN NULL
                    ELSE exit_price/entry_price-1 END AS ret,
               exit_delayed_days,
               entry_blocked, entry_open_at_up_limit, delisted_in_window, beyond_data_end,
               CASE WHEN beyond_data_end THEN 'beyond_data_end'
                    WHEN entry_blocked THEN 'entry_blocked'
                    WHEN delisted_in_window THEN 'delisted_in_window'
                    WHEN exit_delayed THEN 'exit_delayed_days'
                    WHEN entry_open_at_up_limit THEN 'entry_open_at_up_limit'
                    ELSE NULL END AS exception_flag
        FROM resolved
    """


def build_forward_return(store, start=None, end=None, config_root=None) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    labels = research["labels"]
    horizons = labels["horizons"]
    if not isinstance(horizons, list) or not horizons:
        raise ValueError("factor_research.yaml labels.horizons must be a non-empty list")
    if any(not isinstance(h, int) or isinstance(h, bool) or h < 1 for h in horizons):
        raise ValueError("factor_research.yaml labels.horizons must contain positive integers")
    max_delay = labels["max_exit_delay_trading_days"]
    if not isinstance(max_delay, int) or isinstance(max_delay, bool) or max_delay < 0:
        raise ValueError("factor_research.yaml labels.max_exit_delay_trading_days must be a nonnegative integer")
    if labels["exception_flags"] != ["entry_blocked", "entry_open_at_up_limit", "exit_delayed_days",
                                     "delisted_in_window", "beyond_data_end"]:
        raise ValueError("factor_research.yaml labels.exception_flags has an unexpected value")

    states = store.output_relation("l1", "daily_stock_state")
    prices = store.output_relation("l1", "daily_adjusted_price")
    status = store.output_relation("l1", "daily_trade_status")
    calendar = store.output_relation("l1", "daily_calendar")
    # Materialize the grid once: every horizon/label family below reuses it, and
    # exit lookups must be able to look past the requested output window.
    store.db.execute(f"""CREATE OR REPLACE TEMP TABLE label_panel AS
        WITH panel AS (
            SELECT st.ts_code, st.trade_date, c.trade_index,
                   st.is_listed, st.is_delisted,
                   p.trade_date IS NOT NULL AS quotation_exists,
                   p.open, p.adjusted_open, p.adjusted_close,
                   ts.up_limit
            FROM {states} st
            JOIN {calendar} c ON c.trade_date=st.trade_date
            LEFT JOIN {prices} p ON p.trade_date=st.trade_date AND p.ts_code=st.ts_code
            LEFT JOIN {status} ts ON ts.trade_date=st.trade_date AND ts.ts_code=st.ts_code
        ), quotes AS (
            SELECT ts_code, trade_index, trade_date, adjusted_open, adjusted_close
            FROM panel WHERE quotation_exists
        ), sequenced AS (
            SELECT *,
                   min(CASE WHEN quotation_exists THEN trade_index END) OVER (
                       PARTITION BY ts_code ORDER BY trade_index
                       ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS nq_index,
                   max(CASE WHEN quotation_exists THEN trade_index END) OVER (
                       PARTITION BY ts_code ORDER BY trade_index
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS lq_index
            FROM panel
        )
        SELECT s.*,
               qo.trade_date AS nq_date, qo.adjusted_open AS nq_open, qo.adjusted_close AS nq_close,
               ql.trade_date AS lq_date, ql.adjusted_close AS lq_close
        FROM sequenced s
        LEFT JOIN quotes qo ON qo.ts_code=s.ts_code AND qo.trade_index=s.nq_index
        LEFT JOIN quotes ql ON ql.ts_code=s.ts_code AND ql.trade_index=s.lq_index
    """)
    views = []
    for horizon in horizons:
        for family in ("o2o", "c2c"):
            name = f"label_{family}_{horizon}"
            store.db.execute(f"CREATE OR REPLACE TEMP VIEW {name} AS "
                             + _label_view(family, horizon, max_delay, date_window(store, start, end)))
            views.append(name)
    query = " UNION ALL ".join(f"SELECT * FROM {name}" for name in views)
    result = store.publish("l3", "forward_return", query, ["trade_date", "ts_code", "label_id"])
    relation = store.output_relation("l3", "forward_return")
    counts = store.rows(f"""SELECT label_id, coalesce(exception_flag,'none') AS exception_flag,
        count(*) AS n, count(ret) AS with_return
        FROM {relation} GROUP BY 1,2 ORDER BY 1,2""")
    store.write_json(("l3", "forward_return", "source_diagnostics.json"),
                     {"horizons": horizons, "max_exit_delay_trading_days": max_delay,
                      "label_exception_counts": counts})
    return result
