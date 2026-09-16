"""Validate complete source calendars and expose strictly bounded date operations."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re

from core.data.store import date_sql, qi, qs


class CalendarRangeError(ValueError):
    """The source calendar cannot establish the requested trading date."""


def _date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[0-9]{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            return date.fromisoformat(text)
    raise ValueError(f"Invalid calendar date: {value!r}; expected date, YYYYMMDD or YYYY-MM-DD")


@dataclass(frozen=True)
class TradingCalendar:
    trade_dates: tuple[date, ...]
    calendar_start: date
    calendar_end: date
    exchange: str = "SSE"

    def __post_init__(self):
        if not self.trade_dates:
            raise ValueError(f"trade_cal[{self.exchange}]: no open trading dates")
        if tuple(sorted(set(self.trade_dates))) != self.trade_dates:
            raise ValueError("daily_calendar: trade_date must be unique and strictly increasing")
        if (self.calendar_start > self.calendar_end
                or self.trade_dates[0] < self.calendar_start
                or self.trade_dates[-1] > self.calendar_end):
            raise ValueError("daily_calendar: trading dates lie outside calendar_start/calendar_end")

    @classmethod
    def from_rows(cls, rows, exchange: str = "SSE") -> "TradingCalendar":
        selected = {}
        for row in rows:
            if str(row.get("exchange", "")).strip() != exchange:
                continue
            raw_date = row.get("cal_date")
            try:
                day = _date(raw_date)
            except (ValueError, TypeError) as error:
                raise ValueError(f"trade_cal[{exchange}].cal_date is invalid: {raw_date!r}") from error
            if day in selected:
                raise ValueError(f"trade_cal[{exchange}]: duplicate/conflicting cal_date {day}")
            raw_open = row.get("is_open")
            opened = raw_open.strip() if isinstance(raw_open, str) else raw_open
            if opened not in (0, 1, "0", "1"):
                raise ValueError(f"trade_cal[{exchange}].is_open must be 0 or 1: {day}: {raw_open!r}")
            selected[day] = opened in (1, "1")
        if not selected:
            raise ValueError(f"trade_cal: no source rows for exchange {exchange}")
        start, end = min(selected), max(selected)
        day = start
        while day <= end:
            if day not in selected:
                raise ValueError(f"trade_cal[{exchange}]: missing natural calendar date {day}")
            day += timedelta(days=1)
        return cls(tuple(sorted(day for day, opened in selected.items() if opened)), start, end, exchange)

    @classmethod
    def from_store(cls, store) -> "TradingCalendar":
        relation = store.output_relation("l1", "daily_calendar")
        rows = store.rows(f"SELECT * FROM {relation} ORDER BY trade_date")
        if not rows:
            raise ValueError("daily_calendar: no open trading dates")
        expected_exchange = store.rules["calendar_exchange"]
        if {row["exchange"] for row in rows} != {expected_exchange}:
            raise ValueError(f"daily_calendar: expected only exchange {expected_exchange}")
        starts = {_date(row["calendar_start"]) for row in rows}
        ends = {_date(row["calendar_end"]) for row in rows}
        if len(starts) != 1 or len(ends) != 1:
            raise ValueError("daily_calendar: inconsistent natural calendar bounds")
        result = cls(tuple(_date(row["trade_date"]) for row in rows), starts.pop(), ends.pop(), expected_exchange)
        for index, row in enumerate(rows):
            expected_previous = result.trade_dates[index - 1] if index else None
            expected_next = result.trade_dates[index + 1] if index + 1 < len(rows) else None
            if (row["trade_index"] != index + 1 or row["prev_trade_date"] != expected_previous
                    or row["next_trade_date"] != expected_next):
                raise ValueError(f"daily_calendar: inconsistent links/index at {row['trade_date']}")
        return result

    def _bounded(self, value) -> date:
        day = _date(value)
        if day < self.calendar_start or day > self.calendar_end:
            raise CalendarRangeError(
                f"Date {day} is outside {self.exchange} calendar coverage "
                f"[{self.calendar_start}, {self.calendar_end}]"
            )
        return day

    def is_trade_date(self, value) -> bool:
        day = self._bounded(value)
        index = bisect_left(self.trade_dates, day)
        return index < len(self.trade_dates) and self.trade_dates[index] == day

    def previous(self, value) -> date:
        day = self._bounded(value)
        index = bisect_left(self.trade_dates, day) - 1
        if index < 0:
            raise CalendarRangeError(f"No previous trading date before {day} within {self.exchange} calendar coverage")
        return self.trade_dates[index]

    def next(self, value) -> date:
        day = self._bounded(value)
        index = bisect_right(self.trade_dates, day)
        if index == len(self.trade_dates):
            raise CalendarRangeError(f"No next trading date after {day} within {self.exchange} calendar coverage")
        return self.trade_dates[index]

    def offset(self, value, n: int) -> date:
        day = self._bounded(value)
        if not isinstance(n, int) or isinstance(n, bool):
            raise ValueError(f"Trading-date offset must be an integer: {n!r}")
        index = bisect_left(self.trade_dates, day)
        if index == len(self.trade_dates) or self.trade_dates[index] != day:
            raise ValueError(f"Trading-date offset requires a trading date: {day}")
        destination = index + n
        if not 0 <= destination < len(self.trade_dates):
            raise CalendarRangeError(f"Offset {n} from {day} leaves {self.exchange} calendar coverage")
        return self.trade_dates[destination]

    def available_date(self, value) -> date:
        return self.next(value)


def build_calendar(store) -> dict:
    """Validate source natural days before publishing unique, open trading days."""
    raw = store.raw("trade_cal")
    exchange = store.rules["calendar_exchange"]
    source = store.rows(f"SELECT exchange, cal_date, is_open FROM {raw} "
                        f"WHERE trim(cast(exchange AS VARCHAR))={qs(exchange)}")
    TradingCalendar.from_rows(source, exchange)
    query = f"""
        WITH source AS (
            SELECT {date_sql(qi('cal_date'))} AS trade_date,
                   cast(is_open AS INTEGER) AS is_open
            FROM {raw}
            WHERE trim(cast(exchange AS VARCHAR))={qs(exchange)}
        ), bounds AS (
            SELECT min(trade_date) AS calendar_start, max(trade_date) AS calendar_end
            FROM source
        ), opened AS (
            SELECT trade_date FROM source WHERE is_open=1
        )
        SELECT trade_date,
               lag(trade_date) OVER (ORDER BY trade_date) AS prev_trade_date,
               lead(trade_date) OVER (ORDER BY trade_date) AS next_trade_date,
               row_number() OVER (ORDER BY trade_date) AS trade_index,
               calendar_start, calendar_end, {qs(exchange)} AS exchange
        FROM opened CROSS JOIN bounds
    """
    return store.publish("l1", "daily_calendar", query, ["trade_date"])
