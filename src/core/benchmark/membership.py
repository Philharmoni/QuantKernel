"""Daily index constituents expanded from periodic index_weight snapshots.

Stage 2 source directory: this module must never call Store's raw-table
reader (enforced by core.factor.entry.check_no_raw_access); it only reads the L1 output
core.reference.membership.build_index_weight_available already validated.

index_weight ships one row per (index_code, con_code) at each rebalance
trade_date (monthly for most tracked indices). A rebalance's constituent
list only becomes knowable
configs/factor_research.yaml timing.index_weight_publication_lag_trading_days
trading days after that trade_date (tentative, pending confirmation). This
module expands each rebalance into the calendar days during which it was the
latest *available* rebalance for its index -- from its own available_date up
to (but excluding) the next rebalance's available_date -- so no row is ever
visible earlier than the date its underlying weight could actually have been
observed. A rebalance whose lag-adjusted trading day falls beyond the
calendar's coverage never becomes available within the modeled calendar and
is dropped (reported in source_diagnostics.json), not silently extended.
"""
from core.config.settings import load_settings
from core.market.prices import date_window


def build_daily_index_member(store, start=None, end=None, config_root=None) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    lag = research["timing"]["index_weight_publication_lag_trading_days"]
    if not isinstance(lag, int) or isinstance(lag, bool) or lag < 0:
        raise ValueError("factor_research.yaml timing.index_weight_publication_lag_trading_days must be a nonnegative integer")
    calendar = store.output_relation("l1", "daily_calendar")
    weight = store.output_relation("l1", "index_weight_available")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW index_rebalance_windows AS
        WITH rebalances AS (
            SELECT DISTINCT index_code, trade_date AS weight_trade_date FROM {weight}
        ), anchored AS (
            SELECT r.index_code, r.weight_trade_date,
                   (SELECT min(c.trade_date) FROM {calendar} c WHERE c.trade_date>=r.weight_trade_date) AS anchor_date
            FROM rebalances r
        ), available AS (
            SELECT a.index_code, a.weight_trade_date,
                   (SELECT c2.trade_date FROM {calendar} c1 JOIN {calendar} c2
                      ON c2.trade_index=c1.trade_index+{lag}
                    WHERE c1.trade_date=a.anchor_date) AS available_date
            FROM anchored a
        )
        SELECT index_code, weight_trade_date, available_date,
               LEAD(available_date) OVER (PARTITION BY index_code ORDER BY weight_trade_date) AS next_available_date
        FROM available
    """)
    dropped = store.rows("SELECT index_code, weight_trade_date FROM index_rebalance_windows "
                         "WHERE available_date IS NULL ORDER BY index_code, weight_trade_date")
    query = f"""
        WITH windowed AS (
            SELECT w.index_code, w.con_code, w.trade_date AS weight_trade_date, w.weight,
                   r.available_date, r.next_available_date
            FROM {weight} w JOIN index_rebalance_windows r
              ON r.index_code=w.index_code AND r.weight_trade_date=w.trade_date
            WHERE r.available_date IS NOT NULL
        )
        SELECT c.trade_date, wd.index_code, wd.con_code, wd.weight,
               wd.weight_trade_date, wd.available_date,
               'index_weight' AS source_table
        FROM {calendar} c JOIN windowed wd
          ON c.trade_date>=wd.available_date
         AND (wd.next_available_date IS NULL OR c.trade_date<wd.next_available_date)
        WHERE {date_window(store, start, end, 'c.trade_date')}
    """
    result = store.publish("l3", "daily_index_member", query, ["trade_date", "index_code", "con_code"])
    store.write_json(("l3", "daily_index_member", "source_diagnostics.json"),
                     {"publication_lag_trading_days": lag,
                      "rebalances_dropped_beyond_calendar_coverage": dropped})
    return result
