"""Rebuild tiny, wholly fictional Stage 2 CSV inputs; never read project data/.

These fixtures sit one layer "below" tests/fixtures/golden/: they are raw
provider-table-shaped CSVs (same field names as the real TuShare tables) that,
once run through the existing Stage 1 builders, produce the L1/L2 inputs that
Stage 2 modules (industry, label, benchmark, factor, preprocess, evaluation)
will consume. They are a separate, larger fixture from tests/fixtures/golden/
(8 stocks, ~60 trading days) because Stage 2 boundary scenarios (exit delay,
delisting mid-holding-window, locked limit-up at open, industry membership
PIT, market-cap unit conversion, insufficient cross-section, rank ties) need
more calendar room and more stocks than the Stage 1 fixture provides.
"""
import csv
from datetime import date, timedelta
from pathlib import Path


DESTINATION = Path(__file__).resolve().parent
START = date(2020, 3, 2)
STOP = date(2020, 6, 5)
HOLIDAYS = {date(2020, 4, 6)} | {date(2020, 5, day) for day in range(1, 6)}
CODES = ("SA001.SZ", "SA002.SZ", "SA003.SZ", "SA004.SZ",
         "SA005.SZ", "SA006.SZ", "SA007.SZ", "SA008.SZ")
LIST_DATE = "20190101"  # every fixture stock is listed well before the calendar window
BASE_CLOSE = {"SA001.SZ": 100, "SA002.SZ": 100, "SA003.SZ": 90, "SA004.SZ": 95,
              "SA005.SZ": 105, "SA006.SZ": 110, "SA007.SZ": 120, "SA008.SZ": 400}


def write_csv(name, fields, rows):
    with (DESTINATION / name).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build():
    calendar = []
    trading_dates = []
    previous = (START - timedelta(days=3)).strftime("%Y%m%d")
    current = START
    while current <= STOP:
        label = current.strftime("%Y%m%d")
        opened = current.weekday() < 5 and current not in HOLIDAYS
        calendar.append(dict(cal_date=label, exchange="SSE", is_open=int(opened),
                             pretrade_date=previous))
        if opened:
            trading_dates.append(label)
            previous = label
        current += timedelta(days=1)
    write_csv("trade_cal.csv", ("cal_date", "exchange", "is_open", "pretrade_date"), calendar)

    # Scenario anchors, expressed as trading-day indices so the resolved
    # calendar dates are computed (never hand-guessed) and printed below.
    T = 15                    # common "factor observation date" for several scenarios
    ENTRY = T + 1              # 16: T+1 open entry day
    DELIST_IDX = 20            # SA006: last quote at trading_dates[19], delisted from [20]
    EXIT5 = T + 1 + 5          # 21: planned T+1+5 open exit day (h=5)
    EXIT5_RESUME = EXIT5 + 2   # 23: SA005 resumes trading here (2 trading days delayed)
    INDUSTRY_JOIN_IDX = 30     # SA007 joins its SW industry mid-window
    THIN_DAY = 35              # cross-section-insufficient demonstration day

    n = len(trading_dates)
    T_END = n - 21             # so that T_END+1+20 == n (one past the last valid index)

    assert EXIT5_RESUME < INDUSTRY_JOIN_IDX < THIN_DAY < T_END < n, \
        (EXIT5_RESUME, INDUSTRY_JOIN_IDX, THIN_DAY, T_END, n)

    stocks = []
    for code in CODES:
        stocks.append(dict(ts_code=code, symbol=code.split(".")[0], name="Fixture " + code,
                           market="主板", exchange={"SZ": "SZSE", "SH": "SSE"}[code[-2:]],
                           curr_type="CNY", list_status="D" if code == "SA006.SZ" else "L",
                           list_date=LIST_DATE,
                           delist_date=trading_dates[DELIST_IDX] if code == "SA006.SZ" else ""))
    write_csv("stock_basic.csv", ("ts_code", "symbol", "name", "market", "exchange",
                                 "curr_type", "list_status", "list_date", "delist_date"), stocks)

    names = [dict(ts_code=code, name="Fixture " + code, start_date=LIST_DATE, end_date="",
                  ann_date=LIST_DATE, change_reason="其他") for code in CODES]
    write_csv("namechange.csv", ("ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"), names)

    # Per-stock total_share (万股, constant) used for the market-cap unit-conversion
    # check: total_mv (万元) must equal close (元) * total_share (万股) every row.
    total_share = {"SA001.SZ": 10000, "SA002.SZ": 8000, "SA003.SZ": 6000, "SA004.SZ": 7000,
                   "SA005.SZ": 9000, "SA006.SZ": 5000, "SA007.SZ": 4000, "SA008.SZ": 3000}

    prices = []
    for i, trading_date in enumerate(trading_dates):
        for code in CODES:
            close = BASE_CLOSE[code]
            if code == "SA003.SZ" and i in (ENTRY, THIN_DAY):
                continue  # full-day suspension: entry_blocked, then thin-cross-section day
            if code == "SA004.SZ":
                if i >= ENTRY:
                    close = 104.5  # one-word up-limit on the entry day, sustained afterward
            if code == "SA005.SZ":
                if i in (EXIT5, EXIT5 + 1):
                    continue  # suspended while the planned exit day falls due
                if i >= EXIT5_RESUME:
                    close = 115  # price discovery on resumption
            if code == "SA006.SZ" and i >= DELIST_IDX:
                continue  # delisted; no further quotations
            share = total_share[code]
            prices.append(dict(ts_code=code, trade_date=trading_date, open=close, high=close,
                               low=close, close=close, vol=1000, amount=close * 100,
                               adj_factor=1, total_share=share, total_mv=close * share,
                               circ_mv=close * share,
                               # Step 5: constant turnover keeps turn_20 = 2.0 exactly;
                               # pe_ttm=25 / pb=2 give vendor reciprocals 0.04 / 0.5
                               # for the vendor-comparison report (SA001's self-built
                               # ep_ttm at T is 0.05, i.e. a deliberate 25% deviation).
                               turnover_rate=2.0, pe_ttm=25.0, pb=2.0))
    write_csv("stk_factor_pro.csv", ("ts_code", "trade_date", "open", "high", "low", "close",
                                    "vol", "amount", "adj_factor", "total_share", "total_mv", "circ_mv",
                                    "turnover_rate", "pe_ttm", "pb"), prices)

    write_csv("suspend_d.csv", ("ts_code", "trade_date", "suspend_timing", "suspend_type"), [
        dict(ts_code="SA003.SZ", trade_date=trading_dates[ENTRY], suspend_timing="", suspend_type="S"),
        dict(ts_code="SA003.SZ", trade_date=trading_dates[THIN_DAY], suspend_timing="", suspend_type="S"),
        dict(ts_code="SA005.SZ", trade_date=trading_dates[EXIT5], suspend_timing="", suspend_type="S"),
        dict(ts_code="SA005.SZ", trade_date=trading_dates[EXIT5 + 1], suspend_timing="", suspend_type="S"),
    ])
    write_csv("limit_list_d.csv", ("trade_date", "ts_code", "close", "limit"), [
        dict(trade_date=trading_dates[ENTRY], ts_code="SA004.SZ", close=104.5, limit="U"),
    ])
    write_csv("stk_limit.csv", ("trade_date", "ts_code", "up_limit", "down_limit"), [
        dict(trade_date=trading_dates[ENTRY], ts_code="SA004.SZ", up_limit=104.5, down_limit=85.5),
    ])

    # Financial announcements for SA001 only: four quarters at a fixed 10/25/55/100%
    # cumulative shape (of each field's full-year total), with Q4 announced right at
    # the T/T+1 boundary to exercise "financial announcement just before/after T",
    # and a same-day revision of net income announced on T itself (visible at T+1).
    ratios = (0.10, 0.25, 0.55, 1.00)
    ann_idx = (1, 3, 6, 14)  # Q4 announced at trading_dates[14] -> available at T=trading_dates[15]
    # Four consecutive FY2019 quarters (Q4 = the full fiscal year), each strictly
    # before its own announcement date: an announcement before its own report
    # period ends is invalid and never becomes available (core.financial.available),
    # so every end_date here must stay in the past relative to ann_idx.
    end_dates = ("20190331", "20190630", "20190930", "20191231")
    totals = dict(revenue=2_000_000_000, n_income_attr_p=500_000_000,
                  n_cashflow_act=480_000_000, oper_cost=1_400_000_000,
                  operate_profit=600_000_000, total_profit=620_000_000)
    base_fields = ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "update_flag")
    incomes, cashflows, balancesheets, indicators = [], [], [], []
    for q in range(4):
        ann = trading_dates[ann_idx[q]]
        base = dict(ts_code="SA001.SZ", ann_date=ann, f_ann_date=ann,
                    end_date=end_dates[q], report_type="1", update_flag="0")
        incomes.append(dict(base, revenue=round(totals["revenue"] * ratios[q]),
                            n_income_attr_p=round(totals["n_income_attr_p"] * ratios[q]),
                            oper_cost=round(totals["oper_cost"] * ratios[q]),
                            operate_profit=round(totals["operate_profit"] * ratios[q]),
                            total_profit=round(totals["total_profit"] * ratios[q])))
        cashflows.append(dict(base, n_cashflow_act=round(totals["n_cashflow_act"] * ratios[q])))
    # Same-day revision of Q4 net income: announced on T itself, so it is NOT yet
    # visible at T (ann_date==T is excluded) and only becomes visible at T+1.
    revision_ann = trading_dates[T]
    incomes.append(dict(ts_code="SA001.SZ", ann_date=revision_ann, f_ann_date=revision_ann,
                        end_date=end_dates[3], report_type="1", update_flag="1",
                        revenue=round(totals["revenue"] * ratios[3]),
                        n_income_attr_p=520_000_000,
                        oper_cost=round(totals["oper_cost"] * ratios[3]),
                        operate_profit=round(totals["operate_profit"] * ratios[3]),
                        total_profit=round(totals["total_profit"] * ratios[3])))
    # Step 5 growth factors: FY2018 quarters for SA001. Their announcements must
    # fall INSIDE the fixture calendar: Stage 1 marks announcements before the
    # coverage start as calendar_before_coverage (never available), which would
    # silently remove the whole FY2018 base year. Announcing all four on the
    # first fixture trading day makes them available from the next trading day,
    # well before T. Single-quarter Q4 values are FY*(1-0.55) = 45% of the FY
    # total, giving clean hand-computed YoY at T: Q4-2019 revenue 900M vs
    # Q4-2018 450M -> rev_yoy_q = 1.0; Q4-2019 net income 225M vs Q4-2018 180M
    # -> np_yoy_q = 0.25 (245M/180M-1 = 0.36111... once the T+1 revision shows).
    totals_2018 = dict(revenue=1_000_000_000, n_income_attr_p=400_000_000, n_cashflow_act=300_000_000)
    ends_2018 = ("20180331", "20180630", "20180930", "20181231")
    anns_2018 = (trading_dates[0],) * 4
    for q in range(4):
        base18 = dict(ts_code="SA001.SZ", ann_date=anns_2018[q], f_ann_date=anns_2018[q],
                      end_date=ends_2018[q], report_type="1", update_flag="0")
        incomes.append(dict(base18, revenue=round(totals_2018["revenue"] * ratios[q]),
                            n_income_attr_p=round(totals_2018["n_income_attr_p"] * ratios[q])))
        cashflows.append(dict(base18, n_cashflow_act=round(totals_2018["n_cashflow_act"] * ratios[q])))
    balancesheets.append(dict(ts_code="SA001.SZ", ann_date=trading_dates[ann_idx[3]],
                              f_ann_date=trading_dates[ann_idx[3]], end_date=end_dates[3],
                              report_type="1", update_flag="0",
                              total_assets=9_000_000_000, total_hldr_eqy_exc_min_int=5_000_000_000,
                              total_liab=4_000_000_000, total_cur_assets=3_000_000_000,
                              total_cur_liab=1_500_000_000))
    indicators.append(dict(ts_code="SA001.SZ", ann_date=trading_dates[ann_idx[3]],
                           end_date=end_dates[3], update_flag="0", eps="2.50"))
    write_csv("income_vip.csv", base_fields + ("revenue", "n_income_attr_p", "oper_cost",
                                               "operate_profit", "total_profit"), incomes)
    write_csv("cashflow_vip.csv", base_fields + ("n_cashflow_act",), cashflows)
    write_csv("balancesheet_vip.csv", base_fields + ("total_assets", "total_hldr_eqy_exc_min_int",
                                                     "total_liab", "total_cur_assets", "total_cur_liab"), balancesheets)
    write_csv("fina_indicator.csv", ("ts_code", "ann_date", "end_date", "update_flag", "eps"), indicators)

    # Industry membership (fictional classification codes, not real SW codes):
    # SA007 joins mid-window (in_date within the fixture calendar); SA008 is
    # already a member before the window starts (in_date before LIST_DATE's
    # calendar, i.e. before the fixture's first trading date).
    write_csv("index_classify.csv", ("index_code", "industry_name", "parent_code", "level",
                                     "industry_code", "is_pub", "src"), [
        dict(index_code="900001.SI", industry_name="FixtureSectorA", parent_code="0",
             level="L1", industry_code="990001", is_pub="1", src="SW2021"),
        dict(index_code="900002.SI", industry_name="FixtureSectorB", parent_code="0",
             level="L1", industry_code="990002", is_pub="1", src="SW2021"),
    ])
    members = []
    for code in CODES:
        if code == "SA007.SZ":
            l1_code, l1_name, in_date = "900001.SI", "FixtureSectorA", trading_dates[INDUSTRY_JOIN_IDX]
        elif code == "SA008.SZ":
            l1_code, l1_name, in_date = "900002.SI", "FixtureSectorB", "20190101"
        else:
            l1_code, l1_name, in_date = "900001.SI", "FixtureSectorA", "20190101"
        members.append(dict(l1_code=l1_code, l1_name=l1_name, l2_code=l1_code, l2_name=l1_name,
                            l3_code=l1_code, l3_name=l1_name, ts_code=code, name="Fixture " + code,
                            in_date=in_date, out_date="", is_new="Y"))
    write_csv("index_member_all.csv", ("l1_code", "l1_name", "l2_code", "l2_name", "l3_code",
                                       "l3_name", "ts_code", "name", "in_date", "out_date", "is_new"), members)
    # Step 4 benchmark fixture: four 000300.SH members chosen so the self-built
    # return is hand-computable. SA004 jumps 95 -> 104.5 at ENTRY (index16) with
    # quotes on both index15 and index16, so its ret_1d at index16 is exactly
    # 104.5/95-1 = 0.1; SA003 is suspended at index16 (ret_1d NULL -> excluded
    # from both the equal-weight mean and the cap-weight denominator); SA001 /
    # SA002 are flat. Everywhere else all four are flat, so index16 is the only
    # non-zero self-built benchmark return in the fixture.
    write_csv("index_weight.csv", ("index_code", "con_code", "trade_date", "weight"), [
        dict(index_code="000300.SH", con_code="SA001.SZ", trade_date="20200201", weight=5.0),
        dict(index_code="000300.SH", con_code="SA002.SZ", trade_date="20200201", weight=3.0),
        dict(index_code="000300.SH", con_code="SA003.SZ", trade_date="20200201", weight=1.0),
        dict(index_code="000300.SH", con_code="SA004.SZ", trade_date="20200201", weight=4.0),
    ])

    # Official index daily for the same 000300.SH, covering exactly the four
    # trading dates index15..18. The official return equals the self-built
    # equal-weight return scaled by exactly 3 (non-zero only at index16, where
    # the official index also rises 10%), so the calibration Pearson is exactly
    # 1.0 and the tracking error has a closed form (see expected.json).
    index_closes = {15: (1000, 1000), 16: (1100, 1000), 17: (1100, 1100), 18: (1100, 1100)}
    index_rows = []
    for i in sorted(index_closes):
        close, pre_close = index_closes[i]
        index_rows.append(dict(ts_code="000300.SH", trade_date=trading_dates[i],
                               open=close, high=close, low=close, close=close, pre_close=pre_close,
                               change=close - pre_close, pct_chg=round((close / pre_close - 1) * 100, 6),
                               vol=1000000, amount=1000000))
    write_csv("index_daily.csv", ("ts_code", "trade_date", "open", "high", "low", "close",
                                 "pre_close", "change", "pct_chg", "vol", "amount"), index_rows)

    print(f"Rebuilt golden_stage2 fixture: {len(calendar)} calendar dates, {n} trading dates, "
          f"{len(CODES)} stocks")
    print(f"T={trading_dates[T]} (index {T}); ENTRY(T+1)={trading_dates[ENTRY]}; "
          f"EXIT5(T+1+5)={trading_dates[EXIT5]}; EXIT5_RESUME={trading_dates[EXIT5_RESUME]}")
    print(f"SA006 delist_date={trading_dates[DELIST_IDX]} (last quote {trading_dates[DELIST_IDX - 1]})")
    print(f"SA007 industry in_date={trading_dates[INDUSTRY_JOIN_IDX]}")
    print(f"THIN_DAY(cross-section)={trading_dates[THIN_DAY]}")
    print(f"T_END={trading_dates[T_END]} (index {T_END}); T_END+1+20 index={T_END + 1 + 20} "
          f"(last valid index {n - 1}); T_END+1+5 index={T_END + 1 + 5} -> {trading_dates[T_END + 1 + 5]}")
    print(f"Revenue/net-income/cashflow Q4 available_date={trading_dates[T]}; "
          f"revision (520,000,000) available_date={trading_dates[ENTRY]}")


if __name__ == "__main__":
    build()
