"""Rebuild tiny, wholly fictional CSV inputs; never read project data/."""
import csv
from datetime import date, timedelta
from pathlib import Path


DESTINATION = Path(__file__).resolve().parent
START = date(2018, 12, 17)
STOP = date(2019, 2, 8)
HOLIDAYS = {date(2019, 1, 1)} | {date(2019, 2, day) for day in range(4, 9)}
CODES = ("000001.SZ", "000002.SZ", "000003.SZ", "600001.SH", "830001.BJ")


def write_csv(name, fields, rows):
    with (DESTINATION / name).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build():
    calendar = []
    trading_dates = []
    previous = "20181214"
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

    stocks = []
    for code, name in zip(CODES, ("Ordinary sample", "Ordinary ST-history sample",
                                 "Ordinary IPO", "Ordinary delisted", "Ordinary Beijing")):
        stocks.append(dict(ts_code=code, symbol=code.split(".")[0], name=name,
                           market="北交所" if code.endswith("BJ") else "主板",
                           exchange={"SZ": "SZSE", "SH": "SSE", "BJ": "BSE"}[code[-2:]],
                           curr_type="CNY", list_status="D" if code == "600001.SH" else "L",
                           list_date="20190109" if code == "000003.SZ" else "20181217",
                           delist_date="20190123" if code == "600001.SH" else ""))
    write_csv("stock_basic.csv", ("ts_code", "symbol", "name", "market", "exchange",
                                 "curr_type", "list_status", "list_date", "delist_date"), stocks)

    # Historical name intervals mirror the real namechange layout: every span is
    # inclusive on both ends and an empty end_date means "still current".
    name_changes = {
        "000001.SZ": [("20181217", "", "Ordinary sample")],
        "000002.SZ": [("20181217", "20190106", "Ordinary sample two"),
                      ("20190107", "20190109", "ST sample"),
                      ("20190110", "20190113", "*ST sample"),
                      ("20190114", "20190115", "ST sample"),
                      ("20190116", "", "Ordinary sample two")],
        "000003.SZ": [("20190109", "", "Ordinary IPO")],
        "600001.SH": [("20181217", "", "Ordinary delisted")],
        "830001.BJ": [("20181217", "", "Ordinary Beijing")],
    }
    names = [dict(ts_code=code, name=name, start_date=start, end_date=end,
                  ann_date=start, change_reason="其他")
             for code, spans in name_changes.items() for start, end, name in spans]
    write_csv("namechange.csv", ("ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"), names)

    prices = []
    for trading_date in trading_dates:
        for code in CODES:
            if code == "000003.SZ" and trading_date < "20190109":
                continue
            if code == "600001.SH" and trading_date >= "20190123":
                continue
            if code == "000001.SZ" and trading_date in ("20190103", "20190109"):
                continue
            close, factor = 100, 1
            if code == "000001.SZ":
                if trading_date == "20190104":
                    close = 110
                elif trading_date == "20190107":
                    close = 99
                elif trading_date >= "20190108":
                    close, factor = 49.5, 2
            prices.append(dict(ts_code=code, trade_date=trading_date, open=close, high=close,
                               low=close, close=close, vol=1000, amount=close * 100,
                               adj_factor=factor))
    write_csv("stk_factor_pro.csv", ("ts_code", "trade_date", "open", "high", "low", "close",
                                    "vol", "amount", "adj_factor"), prices)
    write_csv("suspend_d.csv", ("ts_code", "trade_date", "suspend_timing", "suspend_type"),
              [dict(ts_code="000001.SZ", trade_date="20190103", suspend_timing="", suspend_type="S")])
    write_csv("limit_list_d.csv", ("trade_date", "ts_code", "close", "limit"),
              [dict(trade_date="20190104", ts_code="000001.SZ", close=110, limit="U"),
               dict(trade_date="20190107", ts_code="000001.SZ", close=99, limit="D")])

    # Daily limit prices: present from 20190104 onward (one row per trading day for
    # 000001.SZ), so absent days stay genuinely unknown. The Beijing placeholder
    # row (99999.99 / 0.0) mimics real provider sentinel values and must be excluded.
    limit_prices = []
    for trading_date in trading_dates:
        if trading_date < "20190104":
            continue
        if trading_date == "20190104":
            up, down = 110, 90
        elif trading_date == "20190107":
            up, down = 109, 99
        else:
            up, down = 54.45, 44.55
        limit_prices.append(dict(trade_date=trading_date, ts_code="000001.SZ",
                                 up_limit=up, down_limit=down))
    limit_prices.append(dict(trade_date="20190116", ts_code="000002.SZ", up_limit=110, down_limit=90))
    limit_prices.append(dict(trade_date="20190116", ts_code="830001.BJ",
                             up_limit=99999.99, down_limit=0.0))
    write_csv("stk_limit.csv", ("trade_date", "ts_code", "up_limit", "down_limit"), limit_prices)

    # Dates and amounts are deliberately artificial to put all PIT edges in a short calendar.
    announcements = {
        "000001.SZ": [("20180331", "20181220", 10, 100, "0.10", "0"),
                       ("20180630", "20181221", 25, 125, "0.25", "0"),
                       ("20180930", "20181222", 55, 155, "0.55", "0"),
                       ("20181231", "20190104", 100, 200, "1.00", "0"),
                       ("20180630", "20190111", 27, 127, "0.27", "1")],
        "000002.SZ": [("20180331", "20181220", 10, 100, "0.10", "0"),
                       ("20180930", "20181222", 55, 155, "0.55", "0"),
                       ("20181231", "20190104", 100, 200, "1.00", "0")],
    }
    incomes, cashflows, balancesheets, indicators = [], [], [], []
    for code, records in announcements.items():
        for end, announcement, cumulative, assets, eps, update in records:
            base = dict(ts_code=code, ann_date=announcement, f_ann_date=announcement,
                        end_date=end, report_type="1", update_flag=update)
            incomes.append(dict(base, revenue=cumulative, n_income_attr_p=cumulative))
            cashflows.append(dict(base, n_cashflow_act=cumulative))
            balancesheets.append(dict(base, total_assets=assets))
            indicators.append(dict(ts_code=code, ann_date=announcement, end_date=end,
                                   update_flag=update, eps=eps))
    base_fields = ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "update_flag")
    write_csv("income_vip.csv", base_fields + ("revenue", "n_income_attr_p"), incomes)
    write_csv("cashflow_vip.csv", base_fields + ("n_cashflow_act",), cashflows)
    write_csv("balancesheet_vip.csv", base_fields + ("total_assets",), balancesheets)
    write_csv("fina_indicator.csv", ("ts_code", "ann_date", "end_date", "update_flag", "eps"), indicators)
    print(f"Rebuilt 11 artificial CSV files: {len(calendar)} calendar dates, {len(trading_dates)} trading dates, {len(CODES)} stocks")


if __name__ == "__main__":
    build()
