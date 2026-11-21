"""交易时段日历（v2.14）：与 NYSE 公布的 2026–2028 休市、提前收盘表核对（ICE 2025-12-23 公告）。"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
import fw_sessions as S  # noqa: E402

PUBLISHED_HOLIDAYS = {
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
           "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
           "2027-09-06", "2027-11-25", "2027-12-24"],
    2028: ["2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04",
           "2028-11-23", "2028-12-25"],                                    # 2028-01-01 是周六：不补休
}
PUBLISHED_EARLY = {2026: ["2026-11-27", "2026-12-24"], 2027: ["2027-11-26"], 2028: ["2028-07-03", "2028-11-24"]}


@pytest.mark.parametrize("year", [2026, 2027, 2028])
def test_nyse_calendar_matches_the_published_schedule(year):
    assert sorted(str(d) for d in S.nyse_holidays(year)) == PUBLISHED_HOLIDAYS[year]
    assert sorted(str(d) for d in S.nyse_early_closes(year)) == PUBLISHED_EARLY[year]


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_regular_session_hours_follow_daylight_saving_and_early_closes():
    cal = S.NyseCalendar()
    assert cal.hours(date(2026, 1, 5)) == (utc(2026, 1, 5, 14, 30), utc(2026, 1, 5, 21, 0))      # 冬令时
    assert cal.hours(date(2026, 7, 6)) == (utc(2026, 7, 6, 13, 30), utc(2026, 7, 6, 20, 0))      # 夏令时
    assert cal.hours(date(2026, 11, 27)) == (utc(2026, 11, 27, 14, 30), utc(2026, 11, 27, 18, 0))  # 提前收盘 13:00
    assert cal.hours(date(2026, 11, 26)) is None and cal.hours(date(2026, 11, 28)) is None        # 感恩节、周六
    ext = S.NyseCalendar(session="extended")
    assert ext.hours(date(2026, 1, 5)) == (utc(2026, 1, 5, 9, 0), utc(2026, 1, 6, 1, 0))


def test_session_state_and_gaps():
    cal = S.NyseCalendar()
    st = cal.status(utc(2026, 11, 20, 20, 30))                  # 周五 15:30 美东
    assert st.is_open and abs(st.minutes_to_close - 30) < 1e-9 and st.gap_after_close_days == 3
    st = cal.status(utc(2026, 11, 25, 15, 0))                   # 感恩节前一天（周三）：下一个交易日是周五
    assert st.is_open and st.gap_after_close_days == 2
    st = cal.status(utc(2026, 11, 21, 12, 0))                   # 周六
    assert not st.is_open and st.next_open == utc(2026, 11, 23, 14, 30)
    st = cal.status(utc(2026, 11, 23, 14, 0))                   # 周一开盘前
    assert not st.is_open and st.next_open == utc(2026, 11, 23, 14, 30)
    closed = S.NyseCalendar(extra_closed_dates=["2026-11-23"])  # 临时休市
    assert not closed.status(utc(2026, 11, 23, 16, 0)).is_open
    assert S.AlwaysOpen().status(utc(2026, 11, 21, 12, 0)).is_open


def test_calendar_registry_is_the_extension_point():
    with pytest.raises(ValueError, match="未知的交易日历"):
        S.make_calendar("hkex")
    S.register_calendar("always_test", lambda **kw: S.AlwaysOpen())
    assert S.make_calendar("always_test").status(utc(2026, 1, 1)).is_open
    with pytest.raises(ValueError, match="regular 或 extended"):
        S.make_calendar("nyse", session="overnight")
