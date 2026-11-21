"""交易时段日历（v2.14）：给“资产类别”接口用。加密货币全天交易；美股永续（例如 Binance USDⓈ-M 的 TradFi 永续 TSLA/USDT:USDT）
本身 24/7 可交易，但标的股票只在美股交易时段有价格：休市时指数价格停在最后一个值，周一开盘、财报后会跳空。框架用这里的日历
决定什么时候允许新开仓、什么时候提前平仓。

只依赖标准库（zoneinfo 处理美东夏令时）。日历是“接口”：用 register_calendar(name, factory) 可以加别的市场。

NYSE 规则（已与 NYSE 公布的 2026–2028 休市与提前收盘表核对，见 tests/test_sessions.py）：
- 常规时段 09:30–16:00 美东；提前收盘日 13:00；extended 模式按 04:00–20:00 美东（盘前盘后，示例值）。
- 休市：元旦、马丁·路德·金纪念日、总统日、耶稣受难日、阵亡将士纪念日、六月节、独立日、劳动节、感恩节、圣诞节；
  周六的节日提前到周五、周日的顺延到周一；元旦落在周六时不补休（NYSE 规则）。
- 提前收盘：独立日前一天（7 月 4 日在周二至周五时）、感恩节次日、平安夜（工作日且不是休市日时）。
- 临时休市（国丧等）不在规则里：在配置的 extra_closed_dates / extra_early_closes 里补。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


# ============================================================== NYSE 节假日规则
def easter(year: int) -> date:
    """公历复活节（匿名算法，Meeus/Jones/Butcher）。"""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lo = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lo) // 451
    month = (h + lo - 7 * m + 114) // 31
    day = ((h + lo - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> dict:
    """{日期: 名称}：这一年 NYSE 全天休市的日期。"""
    h: dict = {}
    ny = date(year, 1, 1)
    if ny.weekday() != 5:                               # 元旦落在周六：不补休
        h[_observed(ny)] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King, Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    h[_observed(date(year, 12, 25))] = "Christmas Day"
    return h


@lru_cache(maxsize=64)
def nyse_early_closes(year: int) -> frozenset:
    """13:00 美东提前收盘的日期。"""
    hol = nyse_holidays(year)
    out = set()
    j4 = date(year, 7, 4)
    if 1 <= j4.weekday() <= 4:                          # 7 月 4 日在周二至周五：前一天提前收盘
        out.add(j4 - timedelta(days=1))
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))   # 感恩节次日
    ce = date(year, 12, 24)
    if ce.weekday() < 5 and ce not in hol:
        out.add(ce)
    return frozenset(d for d in out if d not in hol)


# ============================================================== 日历接口
@dataclass(frozen=True)
class SessionState:
    is_open: bool                       # 现在在交易时段内
    minutes_since_open: float | None    # 今天开盘到现在（不在时段内为 None）
    minutes_to_close: float | None      # 到今天收盘（不在时段内为 None）
    gap_after_close_days: int           # 今天收盘后，下一个交易日是几天后（1 = 次日；周五收盘 = 3；节前更多）
    next_open: datetime | None          # 下一次开盘（UTC）


class Calendar:
    """日历接口：status(now) 返回 SessionState。新市场实现这个接口，再用 register_calendar 注册。"""
    name = "base"

    def status(self, now: datetime) -> SessionState:  # pragma: no cover - 接口
        raise NotImplementedError


class AlwaysOpen(Calendar):
    """加密货币：全天交易，没有收盘。"""
    name = "24x7"

    def status(self, now: datetime) -> SessionState:
        return SessionState(True, None, None, 0, None)


class NyseCalendar(Calendar):
    name = "nyse"

    def __init__(self, session: str = "regular", extra_closed_dates=(), extra_early_closes=()):
        if session not in ("regular", "extended"):
            raise ValueError(f"session 必须是 regular 或 extended，收到 {session!r}")
        self.session = session
        self.extra_closed = {date.fromisoformat(str(d)) for d in extra_closed_dates}
        self.extra_early = {date.fromisoformat(str(d)) for d in extra_early_closes}

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in nyse_holidays(d.year) and d not in self.extra_closed

    def hours(self, d: date) -> tuple[datetime, datetime] | None:
        """这一天的开盘与收盘（UTC）；休市返回 None。"""
        if not self.is_trading_day(d):
            return None
        early = d in nyse_early_closes(d.year) or d in self.extra_early
        if self.session == "regular":
            o, c = time(9, 30), time(13, 0) if early else time(16, 0)
        else:
            o, c = time(4, 0), time(17, 0) if early else time(20, 0)
        return (datetime.combine(d, o, NY).astimezone(UTC), datetime.combine(d, c, NY).astimezone(UTC))

    def next_trading_day(self, d: date) -> date:
        n = d + timedelta(days=1)
        while not self.is_trading_day(n):
            n += timedelta(days=1)
        return n

    def status(self, now: datetime) -> SessionState:
        now = now if now.tzinfo else now.replace(tzinfo=UTC)
        d = now.astimezone(NY).date()
        h = self.hours(d)
        if h and h[0] <= now < h[1]:
            nxt = self.next_trading_day(d)
            return SessionState(True, (now - h[0]).total_seconds() / 60, (h[1] - now).total_seconds() / 60,
                                (nxt - d).days, self.hours(nxt)[0])
        nd = d if h and now < h[0] else self.next_trading_day(d)
        return SessionState(False, None, None, 0, self.hours(nd)[0])


CALENDARS = {"24x7": lambda **kw: AlwaysOpen(), "nyse": lambda **kw: NyseCalendar(**kw)}


def register_calendar(name: str, factory) -> None:
    """注册新的交易日历（接口）：factory(**日历参数) -> Calendar。例如港股、商品期货的交易时段。"""
    CALENDARS[name] = factory


def make_calendar(name: str, **kw) -> Calendar:
    if name not in CALENDARS:
        raise ValueError(f"未知的交易日历 {name!r}；可选 {sorted(CALENDARS)}（新市场用 register_calendar 注册）")
    return CALENDARS[name](**kw)
