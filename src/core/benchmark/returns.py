"""Self-built benchmark daily returns (plan Step 4).

Stage 2 source directory: this module must never call Store's raw-table
reader (enforced by core.factor.entry.check_no_raw_access); it only consumes
L1/L2/L3 products.

Members are taken at T, returns at T+1:

  research universes  daily_research_universe rows with is_in_universe IS TRUE
                      (excluded/unknown rows are never members).
  index universes     L3 daily_index_member rows, i.e. the Step 2 as-of
                      expansion that already respects the index_weight
                      publication lag.

The return is daily_adjusted_price.ret_1d of the T+1 row (adjusted
close-to-close, NULL whenever the previous quote is not exactly T). Members
whose T+1 ret_1d is NULL are excluded from BOTH the equal-weight mean and the
circ_mv-weight denominator -- never counted as 0, never forward-filled, and
the shortfall stays visible in member_count vs return_count. Weights are
circ_mv at the member day T (万元; the unit cancels in the weighted mean).
Days with members but no usable return keep their row with ret=NULL; days
with no members at all produce no row.

Until the official 000300.SH/000905.SH dailies are downloaded (deferred per
user instruction), every series here is a SELF-BUILT APPROXIMATION, never the
official index; see core.benchmark.calibration for the 000852.SH comparison.
"""
from core.config.settings import load_settings
from core.market.prices import date_window


def build_daily_benchmark_return(store, start=None, end=None, config_root=None) -> dict:
    research = load_settings("factor_research.yaml", config_root)
    config = research["benchmark"]
    methods = config["methods"]
    if methods != ["equal_weight", "circ_mv_weight"]:
        raise ValueError("factor_research.yaml benchmark.methods must be [equal_weight, circ_mv_weight]")
    research_universes = config["research_universes"]
    index_universes = config["index_universes"]
    if research["sample"]["default_universe"] not in research_universes:
        raise ValueError("benchmark.research_universes must include sample.default_universe")
    universe = store.output_relation("l2", "daily_research_universe")
    members_index = store.output_relation("l3", "daily_index_member")
    prices = store.output_relation("l1", "daily_adjusted_price")
    calendar = store.output_relation("l1", "daily_calendar")

    member_queries = []
    for universe_id in research_universes:
        member_queries.append(f"""SELECT trade_date AS member_date, ts_code,
            '{universe_id}' AS benchmark_id FROM {universe}
            WHERE universe_id='{universe_id}' AND is_in_universe IS TRUE""")
    for item in index_universes:
        member_queries.append(f"""SELECT trade_date AS member_date, con_code AS ts_code,
            '{item['benchmark_id']}' AS benchmark_id FROM {members_index}
            WHERE index_code='{item['index_code']}'""")
    members_sql = " UNION ALL ".join(member_queries)
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW benchmark_members AS {members_sql}""")

    # c.trade_date is the return day T+1; c.prev_trade_date is the member day T.
    joined = f"""
        SELECT c.trade_date, m.benchmark_id, m.member_date, m.ts_code,
               p.ret_1d, try_cast(pm.circ_mv AS DOUBLE) AS circ_mv
        FROM benchmark_members m
        JOIN {calendar} c ON c.prev_trade_date=m.member_date
        LEFT JOIN {prices} p ON p.trade_date=c.trade_date AND p.ts_code=m.ts_code
        LEFT JOIN {prices} pm ON pm.trade_date=m.member_date AND pm.ts_code=m.ts_code
        WHERE {date_window(store, start, end, 'c.trade_date')}
    """
    store.db.execute(f"CREATE OR REPLACE TEMP VIEW benchmark_joined AS {joined}")
    # Deterministic aggregation (configs/factor_research.yaml
    # repeatability.preferred_method=fixed_sort_order): DOUBLE sum/avg depend on
    # parallel aggregation order and are NOT bit-reproducible across runs, so
    # the summations are done in exact DECIMAL (order-independent) and only the
    # final quotient is cast back to DOUBLE. Per-element products stay DOUBLE,
    # which is deterministic per row regardless of order.
    equal_return = ("cast(sum(cast(ret_1d AS DECIMAL(38,18))) "
                    "/ nullif(count(ret_1d),0) AS DOUBLE)")
    weighted_return = """cast(
        sum(CASE WHEN ret_1d IS NOT NULL AND circ_mv>0 THEN cast(ret_1d*circ_mv AS DECIMAL(38,18)) END)
        / nullif(sum(CASE WHEN ret_1d IS NOT NULL AND circ_mv>0 THEN cast(circ_mv AS DECIMAL(38,18)) END),0)
        AS DOUBLE)"""
    query = f"""
        SELECT trade_date, benchmark_id, 'equal_weight' AS method,
               min(member_date) AS member_date,
               {equal_return} AS ret,
               count(*) AS member_count, count(ret_1d) AS return_count
        FROM benchmark_joined GROUP BY trade_date, benchmark_id
        UNION ALL
        SELECT trade_date, benchmark_id, 'circ_mv_weight' AS method,
               min(member_date) AS member_date,
               {weighted_return} AS ret,
               count(*) AS member_count, count(ret_1d) AS return_count
        FROM benchmark_joined GROUP BY trade_date, benchmark_id
    """
    result = store.publish("l3", "daily_benchmark_return", query,
                           ["trade_date", "benchmark_id", "method"])
    relation = store.output_relation("l3", "daily_benchmark_return")
    coverage = store.rows(f"""SELECT benchmark_id, method, count(*) AS days,
        min(trade_date) AS first_date, max(trade_date) AS last_date,
        count(ret) AS days_with_return, min(member_count) AS min_members,
        sum(member_count-return_count) AS member_days_without_return
        FROM {relation} GROUP BY 1,2 ORDER BY 1,2""")
    store.write_json(("l3", "daily_benchmark_return", "source_diagnostics.json"),
                     {"basis": "self_built_approximation_not_official_index",
                      "coverage": coverage})
    return result
