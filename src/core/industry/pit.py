"""Daily industry classification with explicit point-in-time provenance.

Stage 2 source directory: this module must never call Store's raw-table
reader (enforced by core.factor.entry.check_no_raw_access); it only reads the L1 output
core.reference.membership.build_index_member_available already validated.

index_member_all currently ships only a single is_new='Y' snapshot per
(ts_code, l1_code) with out_date always empty (see
configs/data_middle_layer.yaml capabilities.industry_pit_history) -- no
historical is_new='N' intervals exist yet. Given that single interval
[in_date, out_date), each (trade_date, ts_code) row is assigned exactly one
of four industry_pit_status values (configs/factor_research.yaml
industry.pit_statuses):

  snapshot_after_in_date  trade_date is within [in_date, out_date); in_date
                          itself is a real observed transition fact, so the
                          classification is used as-is.
  snapshot_backfilled     trade_date < in_date; the classification is only
                          known from the current snapshot and is backfilled
                          onto an earlier date, carrying an explicit
                          forward-looking-risk flag.
  unknown                 trade_date is on/after a recorded out_date (no
                          current evidence of membership).
  pit_history             reserved for when historical is_new='N' intervals
                          are ingested; never emitted today.

Callers pick a policy by filtering on industry_pit_status: strict analyses
keep only configs/factor_research.yaml industry.strict_usable_statuses rows
(treat the rest as unknown/null); backfilled analyses may additionally keep
backfilled_usable_statuses rows.

Coverage is intentionally limited to ts_codes present in
index_member_available: a stock entirely absent from that source table has
no row here at all (equivalent to "unknown" for every trade_date), rather
than being materialized as an explicit row -- downstream consumers that need
full-universe coverage must LEFT JOIN against core.universe output and treat
missing matches as unknown.
"""
from core.config.settings import load_settings
from core.market.prices import date_window

_LEVEL_COLUMNS = ["l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name"]


def build_daily_industry(store, start=None, end=None, config_root=None) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    statuses = research["industry"]["pit_statuses"]
    if statuses != ["pit_history", "snapshot_after_in_date", "snapshot_backfilled", "unknown"]:
        raise ValueError("factor_research.yaml industry.pit_statuses has an unexpected value")
    calendar = store.output_relation("l1", "daily_calendar")
    member = store.output_relation("l1", "index_member_available")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW industry_member AS
        SELECT ts_code, l1_code, l1_name, l2_code, l2_name, l3_code, l3_name, in_date, out_date, is_new
        FROM {member}
    """)
    samples = store.rows("""SELECT ts_code, count(*) AS row_count FROM industry_member
        GROUP BY ts_code HAVING count(*)>1 LIMIT 10""")
    if samples:
        raise ValueError("daily_industry: index_member_available has more than one row for a "
                         f"ts_code; multi-interval industry history is not yet supported: {samples}")
    value_columns = ",\n               ".join(
        f"CASE WHEN m.out_date IS NOT NULL AND c.trade_date>=m.out_date THEN NULL "
        f"ELSE m.{column} END AS {column}" for column in _LEVEL_COLUMNS)
    query = f"""
        SELECT c.trade_date, m.ts_code,
               {value_columns},
               m.in_date, m.out_date, m.is_new,
               CASE WHEN m.out_date IS NOT NULL AND c.trade_date>=m.out_date THEN 'unknown'
                    WHEN c.trade_date<m.in_date THEN 'snapshot_backfilled'
                    ELSE 'snapshot_after_in_date' END AS industry_pit_status,
               'index_member_all' AS source_table
        FROM {calendar} c CROSS JOIN industry_member m
        WHERE {date_window(store, start, end, 'c.trade_date')}
    """
    result = store.publish("l3", "daily_industry", query, ["trade_date", "ts_code"])
    status_counts = store.rows(f"""SELECT industry_pit_status, count(*) AS n
        FROM {store.output_relation("l3", "daily_industry")} GROUP BY 1 ORDER BY 1""")
    store.write_json(("l3", "daily_industry", "source_diagnostics.json"),
                     {"industry_pit_status_counts": status_counts})
    return result
