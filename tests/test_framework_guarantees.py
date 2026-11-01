"""框架保证的故障与边界测试（v2.6，针对外部审查报告的 P0 问题）。

每个测试对应一条“框架保证”在出错或边界条件下的行为：
- 止损随订单绑定（enter_tag），不再在成交时重新找信号；
- 成交价更优、成交价低于止损、缺计划、风险超预算、重启后恢复；
- 入场检查出错一律拒绝；事件日历无效（来源失败、时间非法、过期、缺文件、覆盖不足）一律拒绝。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
import framework_base as F  # noqa: E402

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
EQUITY = 10_000.0


class S(F.FrameworkStrategy):
    timeframe = "15m"
    is_template = False

    def populate_indicators(self, df, md):
        return df

    def populate_entry_trend(self, df, md):
        return df

    def populate_exit_trend(self, df, md):
        return df


class FakeTrade:
    def __init__(self, enter_tag, open_rate, amount, pair="BTC/USDT:USDT", leverage=10.0, entries=1):
        self.enter_tag, self.open_rate, self.amount, self.pair = enter_tag, open_rate, amount, pair
        self.leverage, self.nr_of_successful_entries, self.entry_side = leverage, entries, "buy"
        self.stake_amount = amount * open_rate / leverage
        self.open_date_utc = NOW - timedelta(hours=1)
        self.cd: dict = {}

    def get_custom_data(self, k):
        return self.cd.get(k)

    def set_custom_data(self, k, v):
        self.cd[k] = v


def strat(params=None, open_trades=(), signal_rows=None, closed_trades=(), config=None):
    s = S.__new__(S)
    s.config = dict(config or {})
    s.framework_params = {"event_file_required": False, **(params or {})}
    df = pd.DataFrame(signal_rows or [])
    s.dp = SimpleNamespace(get_analyzed_dataframe=lambda p, tf: (df, None), get_pair_dataframe=lambda p, tf: pd.DataFrame(),
                           runmode=SimpleNamespace(value="dry_run"))
    s.wallets = SimpleNamespace(get_total_stake_amount=lambda: EQUITY)
    F.Trade.get_trades_proxy = staticmethod(lambda is_open=True, **kw: list(open_trades) if is_open else list(closed_trades))
    F.FrameworkStrategy._events_cache = (None, None)
    return s


def tag(stop, cap=None, user="", side="long"):
    return F.encode_plan(stop, cap, NOW - timedelta(minutes=15), user, side=side)


def sized_amount(s, rate, stop):
    """按框架自己的定仓与杠杆算出的数量（模拟 freqtrade 下单）。"""
    lev = s.leverage("BTC/USDT:USDT", NOW, rate, 10, 75, tag(stop), "long")
    stake = s.custom_stake_amount("BTC/USDT:USDT", NOW, rate, 0, 0, 1e9, lev, tag(stop), "long")
    return stake * lev / rate, lev


# ------------------------------------------------------------------ 计划随订单保存
def test_plan_roundtrip_and_advise_entry_keeps_user_tag():
    p = F.decode_plan(F.encode_plan(97.5, 100.1, NOW, "my|tag"))
    assert p["stop"] == 97.5 and p["cap"] == 100.1 and p["user"] == "my|tag"
    assert F.decode_plan("sma_cross") is None and F.decode_plan(F.encode_plan(float("nan"), None, NOW)) is None
    s = strat()
    df = pd.DataFrame({"date": pd.date_range(NOW, periods=3, freq="15min"), "close": [100.0] * 3,
                       "enter_long": [0, 1, 1], "fw_stop": [99.0, 98.0, 97.0], "fw_entry_cap": [None, 100.1, None]})
    S.populate_entry_trend = lambda self, d, md: d.assign(enter_tag=["", "u1", ""])
    try:
        out = s.advise_entry(df, {"pair": "BTC/USDT:USDT"})
    finally:
        S.populate_entry_trend = lambda self, d, md: d
    plans = [F.decode_plan(t) for t in out["enter_tag"]]
    assert plans[0] is None and plans[1]["stop"] == 98.0 and plans[1]["cap"] == 100.1 and plans[1]["user"] == "u1"
    assert plans[2]["stop"] == 97.0 and plans[2]["cap"] is None


def test_stop_comes_from_the_order_not_from_a_newer_signal():
    """审查 P0：挂单期间出现止损 3% 的新信号，旧实现成交时换成新止损（风险 1.10%，止损贴近强平价）。"""
    newer = [{"date": NOW - timedelta(minutes=15), "close": 100.0, "enter_long": 1, "fw_stop": 97.0}]
    s = strat(signal_rows=newer)
    amount, lev = sized_amount(s, 100.0, 99.0)
    t = FakeTrade(tag(99.0), 100.0, amount, leverage=lev)
    s.order_filled(t.pair, t, SimpleNamespace(ft_order_side="buy"), NOW)
    assert t.cd["stop_px"] == 99.0
    assert t.amount * (100.0 - t.cd["stop_px"]) <= EQUITY * 0.005 + 1e-9


def test_better_fill_widens_stop_to_floor_within_budget():
    """审查 P0：不设上限、计划 100 / 止损 99、实际成交 99.5，旧实现 stop_px = None（只剩 −99% 兜底）。"""
    s = strat()
    amount, lev = sized_amount(s, 100.0, 99.0)
    t = FakeTrade(tag(99.0), 99.5, amount, leverage=lev)
    s.order_filled(t.pair, t, SimpleNamespace(ft_order_side="buy"), NOW)
    sp = t.cd["stop_px"]
    assert sp is not None and (99.5 - sp) / 99.5 * 100 >= 0.8 and t.cd.get("stop_widened")
    assert t.amount * (99.5 - sp) <= EQUITY * 0.005 * 1.05


def test_order_price_below_stop_is_rejected_before_ordering():
    """审查 P0：上限 100、止损 99、下单价 98，旧实现 confirm 返回 True、新增风险为负。"""
    s = strat()
    t = tag(99.0, cap=100.0)
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 10, 98.0, "GTC", NOW, t, "long") is False
    assert s.custom_stake_amount("BTC/USDT:USDT", NOW, 98.0, 100, 0, 1e9, 10, t, "long") == 0
    assert s.custom_entry_price("BTC/USDT:USDT", None, NOW, 101.0, t, "long") == 100.0


@pytest.mark.parametrize("enter_tag,fill,amount,reason", [
    ("manual", 100.0, 1.0, "no_plan"),                        # 没有框架计划（例如手动开仓）
    (None, 100.0, 1.0, "no_plan"),
    ("PLAN:99", 98.5, 1.0, "stop_not_below_fill"),            # 跳空成交在止损之下
    ("PLAN:99", 100.0, 1000.0, "risk_over_budget"),           # 数量远超预算
])
def test_invalid_positions_exit_immediately(enter_tag, fill, amount, reason):
    s = strat()
    t = FakeTrade(tag(99.0) if enter_tag == "PLAN:99" else enter_tag, fill, amount)
    s.order_filled(t.pair, t, SimpleNamespace(ft_order_side="buy"), NOW)
    assert t.cd.get("stop_px") is None and t.cd["fw_invalid"] == reason
    assert s.custom_exit(t.pair, t, NOW, fill, 0.0) == f"fw_invalid_stop:{reason}"
    assert s.custom_stoploss(t.pair, t, NOW, fill, 0.0, False) is None


def test_restart_rebinds_from_enter_tag():
    """重启后 order_filled 不会重放；custom_stoploss 第一次调用时从 enter_tag 绑定同一份计划。"""
    s = strat()
    amount, lev = sized_amount(s, 100.0, 98.5)
    t = FakeTrade(tag(98.5), 100.0, amount, leverage=lev)
    fresh = strat()                                       # 新进程：没有任何内存状态
    sl = fresh.custom_stoploss(t.pair, t, NOW, 100.0, 0.0, False)
    assert t.cd["stop_px"] == 98.5 and sl is not None and abs(sl) > 0


def test_unfilled_trade_is_not_bound_or_exited():
    s = strat()
    t = FakeTrade(tag(99.0), 100.0, 1.0, entries=0)
    assert s._bind_stop(t) is None and "fw_invalid" not in t.cd


# ------------------------------------------------------------------ 入场检查出错一律拒绝
def test_exceptions_in_entry_checks_reject(monkeypatch):
    from freqtrade.strategy.strategy_wrapper import strategy_safe_wrapper

    s = strat()
    monkeypatch.setattr(S, "_news_block", lambda self, p, n: (_ for _ in ()).throw(RuntimeError("boom")))
    logging.disable(logging.CRITICAL)
    try:
        assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False
        wrapped = strategy_safe_wrapper(s.confirm_trade_entry, default_retval=True)     # freqtrade 的调用方式
        assert wrapped(pair="BTC/USDT:USDT", order_type="limit", amount=1, rate=100.0, time_in_force="GTC",
                       current_time=NOW, entry_tag=tag(99.0), side="long") is False
        monkeypatch.setattr(S, "_plan_stop", lambda self, t, r: (_ for _ in ()).throw(RuntimeError("boom")))
        assert s.custom_stake_amount("BTC/USDT:USDT", NOW, 100.0, 500, 0, 1e9, 10, tag(99.0), "long") == 0
        assert s.leverage("BTC/USDT:USDT", NOW, 100.0, 10, 75, tag(99.0), "long") == 1.0
    finally:
        logging.disable(logging.NOTSET)


# ------------------------------------------------------------------ 事件日历有效性
def _cal(tmp_path, ok=True, events=None, sources=None, generated=None, horizon=35, name="events.json"):
    f = tmp_path / name
    data = {"generated_at": (generated or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "horizon_days": horizon, "ok": ok, "events": events if events is not None else [],
            "sources": sources if sources is not None else {"fed": {"ok": ok, "count": 1}}}
    f.write_text(json.dumps(data), "utf-8")
    return str(f)


def _confirm(s, now=None):
    return s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", now or datetime.now(timezone.utc),
                                 tag(98.0), "long")


@pytest.mark.parametrize("case", ["ok_false_empty", "all_failed", "failed_no_fallback", "bad_time", "bad_json",
                                  "missing_file", "no_file_configured", "stale", "horizon"])
def test_calendar_problems_reject_entry(tmp_path, case):
    now = datetime.now(timezone.utc)
    f = {"ok_false_empty": lambda: _cal(tmp_path, ok=False, sources={"fed": {"ok": False, "count": 0}}),
         "all_failed": lambda: _cal(tmp_path, ok=False, sources={"fed": {"ok": False, "count": 3},
                                                                  "bls": {"ok": False, "count": 2}}),
         "failed_no_fallback": lambda: _cal(tmp_path, ok=False, sources={"fed": {"ok": True, "count": 3},
                                                                          "bls": {"ok": False, "count": 0}}),
         "bad_time": lambda: _cal(tmp_path, events=[{"time_utc": "not-a-time"}]),
         "bad_json": lambda: (tmp_path / "e.json").write_text("{broken", "utf-8") and str(tmp_path / "e.json"),
         "missing_file": lambda: str(tmp_path / "nope.json"),
         "no_file_configured": lambda: "",
         "stale": lambda: _cal(tmp_path),
         "horizon": lambda: _cal(tmp_path, generated=now - timedelta(days=40)),
         }[case]()
    if case == "stale":
        os.utime(f, (time.time() - 5 * 86400,) * 2)
    s = strat({"event_file": f, "event_file_required": True})
    assert _confirm(s, now) is False
    ok, reason, _ = s._calendar(now)
    assert not ok and reason


def test_calendar_partial_failure_with_fallback_allows(tmp_path):
    f = _cal(tmp_path, ok=False, sources={"fed": {"ok": True, "count": 8}, "bls": {"ok": False, "count": 5}})
    s = strat({"event_file": f, "event_file_required": True})
    assert _confirm(s) is True


def test_calendar_window_blocks_and_backtest_mode_never_rejects_on_file_problems(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    f = _cal(tmp_path, events=[{"time_utc": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}])
    s = strat({"event_file": f, "event_file_required": True})
    assert _confirm(s, now) is False and _confirm(s, now + timedelta(hours=5)) is True
    bad = _cal(tmp_path, ok=False, sources={"fed": {"ok": False, "count": 0}}, name="bad.json")
    s2 = strat({"event_file": bad, "event_file_required": False})                      # 回测：只用时间，不因文件拒绝
    assert _confirm(s2, now) is True


# ------------------------------------------------------------------ 第 2 批：分批止盈按成交确认；总风险含挂单；缺数据即拒绝
class TPStrat(S):
    def take_profit_legs(self, trade):
        return [(2.0, 0.5, "tp_2R")]


def tp_trade():
    s = TPStrat.__new__(TPStrat)
    s.framework_params = {"event_file_required": False}
    s.wallets = SimpleNamespace(get_total_stake_amount=lambda: EQUITY)
    t = FakeTrade(tag(99.0), 100.0, 10.0)
    t.has_open_orders = False
    s._bind_stop(t)
    return s, t


def adjust(s, t, px):
    return s.adjust_trade_position(t, NOW, px, 0, 0, 1e9, px, 0, px, 0)


def test_take_profit_is_done_only_after_the_fill():
    """审查 P1：旧实现下单前就把这一档标记完成，订单没成交时永久跳过。"""
    s, t = tp_trade()
    first = adjust(s, t, 102.5)
    assert first is not None and first[1] == "tp_2R" and abs(first[0] + t.stake_amount * 0.5) < 1e-9
    second = adjust(s, t, 102.5)                         # 订单被拒 / 取消：数量没变 → 再下一次
    assert second is not None and "tp_2R" not in (t.cd.get("tp_done") or [])
    t.has_open_orders = True                             # 有挂着的单：等待，不重复下单
    assert adjust(s, t, 102.5) is None
    t.has_open_orders, t.amount = False, 5.0              # 成交了一半仓位
    assert adjust(s, t, 103.0) is None and t.cd["tp_done"] == ["tp_2R"]


def test_partial_fill_requests_only_the_rest():
    s, t = tp_trade()
    t.amount = 8.0                                       # 第一次只成交了 2（初始 10，目标留 5）
    req = adjust(s, t, 102.5)
    assert req is not None and abs(-req[0] / t.stake_amount * t.amount - 3.0) < 1e-9


def test_take_profit_gives_up_after_max_attempts():
    s, t = tp_trade()
    for _ in range(TPStrat.TP_MAX_ATTEMPTS):
        assert adjust(s, t, 102.5) is not None
    assert adjust(s, t, 102.5) is None and t.cd["tp_skipped"] == ["tp_2R"]


_TID = iter(range(1000, 100000))


def real_trade(state, qty=10.0, enter_tag=None, stop_px=None, pair="ETH/USDT:USDT", invalid=None, price=100.0, is_short=False):
    """用 freqtrade 自己的 LocalTrade / Order 构造交易（v2.6 的测试用了自写假对象，漏掉了挂单 amount = 0 的真实形态）。
    state：pending（挂单未成交）/ partial（部分成交、单子仍挂着）/ filled（入场已完成）。"""
    from freqtrade.persistence import LocalTrade, Order
    from freqtrade.persistence.custom_data import CustomDataWrapper

    CustomDataWrapper.use_db = False
    filled = {"pending": 0.0, "partial": qty * 0.4, "filled": qty}[state]
    t = LocalTrade(id=next(_TID), pair=pair, stake_amount=qty * price / 10, amount=qty if state == "filled" else 0.0,
                   amount_requested=qty, open_rate=price, open_rate_requested=price, fee_open=0.0005, fee_close=0.0005,
                   open_date=NOW, is_open=True, enter_tag=enter_tag, exchange="binance", leverage=10, trading_mode="futures",
                   is_short=is_short)
    side = "sell" if is_short else "buy"
    t.orders.append(Order(ft_order_side=side, ft_pair=pair, ft_is_open=state != "filled", ft_amount=qty, ft_price=price,
                          order_id=f"o{t.id}", status="closed" if state == "filled" else "open", symbol=pair,
                          order_type="limit", side=side, price=price, average=price, amount=qty, filled=filled,
                          remaining=qty - filled, cost=filled * price, order_date=NOW))
    if stop_px is not None:
        t.set_custom_data("stop_px", stop_px)
    if invalid:
        t.set_custom_data("fw_invalid", invalid)
    return t


def test_real_freqtrade_shapes():
    p, q, f = real_trade("pending"), real_trade("partial"), real_trade("filled")
    assert p.amount == 0 and q.amount == 0 and f.amount == 10
    assert p.nr_of_successful_entries == 0 and q.nr_of_successful_entries == 0 and f.nr_of_successful_entries == 1
    assert not q.has_open_position and q.has_open_orders                      # 部分成交时交易所上没有止损


@pytest.mark.parametrize("trade,risk", [
    (lambda: real_trade("filled", enter_tag=tag(99.0), stop_px=99.0), 10.0),
    (lambda: real_trade("pending", enter_tag=tag(99.0)), 10.0),               # v2.6 按 0 计算
    (lambda: real_trade("partial", enter_tag=tag(99.0)), 10.0),               # 已成交 + 剩余，按下单数量算一次
    (lambda: real_trade("pending", enter_tag=tag(97.5), qty=40.0), 100.0),
])
def test_open_risk_uses_requested_amount_for_orders_in_flight(trade, risk):
    s = strat()
    assert abs(s._open_risk([trade()]) - risk) < 1e-9


@pytest.mark.parametrize("trade", [
    lambda: real_trade("filled", enter_tag="manual"),                         # 已成交但没有止损 / 计划
    lambda: real_trade("pending", enter_tag=None),                            # 挂单没有计划
    lambda: real_trade("pending", enter_tag=tag(99.0), invalid="stop_not_below_fill"),
    lambda: real_trade("pending", enter_tag=tag(95.0)),                       # 计划止损按下单价已不合规（超过上限）
])
def test_open_risk_unknown_blocks_new_entries(trade):
    s = strat(open_trades=[trade()])
    assert s._open_risk([trade()]) is None
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False


def test_three_pending_orders_cannot_exceed_open_risk():
    """审查 v2.6 的反例：单笔预算 1%、组合上限 1.5%，三笔正常定仓的挂单，第三笔不能再获准（参数只用于构造反例）。"""
    params = {"risk_per_trade": 0.01, "max_open_risk": 0.015}
    s = strat(params)
    amount, _ = sized_amount(s, 100.0, 99.0)
    first = real_trade("pending", qty=amount, enter_tag=tag(99.0), pair="ETH/USDT:USDT")
    d1 = pd.DataFrame({"date": pd.date_range("2026-09-01", periods=40, freq="D", tz="UTC"),
                       "close": [100 + (i % 3) for i in range(40)]})
    s = strat(params, open_trades=[first])
    rng = {p: np.random.default_rng(i) for i, p in enumerate(["BTC/USDT:USDT", "ETH/USDT:USDT"])}
    data = {p: d1.assign(close=100 * np.exp(np.cumsum(r.normal(0, 0.02, 40)))) for p, r in rng.items()}
    s.dp = SimpleNamespace(get_pair_dataframe=lambda p, tf: data[p], current_whitelist=lambda: [],
                           runmode=SimpleNamespace(value="dry_run"))
    assert s._corr_ok("BTC/USDT:USDT", [first])                             # 相关性不是拒绝原因
    assert abs(s._open_risk([first]) - amount * 1.0) < 1e-9
    half = real_trade("pending", qty=amount / 4, enter_tag=tag(99.0), pair="ETH/USDT:USDT")
    s2 = strat(params, open_trades=[half])
    s2.dp = s.dp
    assert s2.confirm_trade_entry("BTC/USDT:USDT", "limit", amount, 100.0, "GTC", NOW, tag(99.0), "long") is True
    second = real_trade("pending", qty=amount, enter_tag=tag(99.0), pair="SOL/USDT:USDT")
    s3 = strat(params, open_trades=[first, second])
    data["SOL/USDT:USDT"] = d1.assign(close=100 * np.exp(np.cumsum(np.random.default_rng(9).normal(0, 0.02, 40))))
    s3.dp = s.dp
    assert s3._open_risk([first, second]) + amount * 1.0 > EQUITY * 0.015    # 两笔挂单 147 + 第三笔 73.5 > 150
    assert s3._corr_ok("BTC/USDT:USDT", [first, second])
    assert s3.confirm_trade_entry("BTC/USDT:USDT", "limit", amount, 100.0, "GTC", NOW, tag(99.0), "long") is False


def test_partial_fill_cancels_the_rest_so_the_stop_can_be_placed():
    """审查 v2.6 P0：部分成交、单子仍挂着时 freqtrade 不更新 amount，交易所上没有止损。框架撤掉剩余部分。"""
    s = strat()
    q, p = real_trade("partial", enter_tag=tag(99.0)), real_trade("pending", enter_tag=tag(99.0))
    assert s.check_entry_timeout(q.pair, q, q.orders[0], NOW) is True
    assert s.check_entry_timeout(p.pair, p, p.orders[0], NOW) is False      # 还没成交：照常等待
    assert s._bind_stop(q) is None                                           # 撤单完成前不绑定（没有仓位）
    # freqtrade 撤单后：订单关闭、按已成交数量更新仓位 → 下一轮绑定止损
    o = q.orders[0]
    o.ft_is_open, o.status, q.amount = False, "canceled", o.filled
    assert q.nr_of_successful_entries == 1 and q.has_open_position
    sp = s._bind_stop(q)
    assert sp is not None and abs(q.amount * (100.0 - sp)) <= EQUITY * 0.005 * 1.05


def test_correlation_check_rejects_when_daily_data_is_missing():
    """审查 P1：日线缺失时旧实现直接跳过相关性检查。现在拒绝，并在 informative_pairs 里固定订阅日线。"""
    s = strat(open_trades=[real_trade("filled", enter_tag=tag(99.0), stop_px=99.0)])
    s.dp = SimpleNamespace(get_pair_dataframe=lambda p, tf: pd.DataFrame(), current_whitelist=lambda: ["BTC/USDT:USDT"],
                           runmode=SimpleNamespace(value="dry_run"))
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False
    assert s.informative_pairs() == [("BTC/USDT:USDT", "1d")]
    same = pd.DataFrame({"date": pd.date_range("2026-09-01", periods=40, freq="D", tz="UTC"),
                         "close": [100 * 1.01 ** i * (1 + 0.02 * (i % 2)) for i in range(40)]})
    s.dp = SimpleNamespace(get_pair_dataframe=lambda p, tf: same, runmode=SimpleNamespace(value="dry_run"))
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False  # 相关 = 1


def test_take_profit_tolerates_contract_rounding():
    s, t = tp_trade()
    t.amount = 10.0 * (1 - 0.4987)                       # 交易所把 0.01855 取整成 0.0185：只差一点也算完成
    assert adjust(s, t, 102.5) is None and t.cd["tp_done"] == ["tp_2R"]


@pytest.mark.parametrize("policy,expect", [("widen", "widen"), ("exit", "exit"), ("keep", "keep")])
def test_better_fill_policy(policy, expect):
    """成交价比下单价更优、止损距离不足下限时的处理（v2.7 改为参数，由你决定）。"""
    s = strat({"better_fill_policy": policy})
    amount, lev = sized_amount(s, 100.0, 99.0)
    t = real_trade("filled", qty=amount, enter_tag=tag(99.0), pair="BTC/USDT:USDT", price=99.5)
    sp = s._bind_stop(t)
    if expect == "widen":
        assert sp is not None and (99.5 - sp) / 99.5 * 100 >= 0.8 and t.get_custom_data("stop_widened")
    elif expect == "exit":
        assert sp is None and t.get_custom_data("fw_invalid") == "stop_too_close_after_fill"
        assert s.custom_exit(t.pair, t, NOW, 99.5, 0.0) == "fw_invalid_stop:stop_too_close_after_fill"
    else:
        assert sp == 99.0 and t.get_custom_data("stop_kept_below_min")


# ------------------------------------------------------------------ v2.8：外部审查 v2.7
def test_floor_mode_pending_risk_matches_sizing():
    """审查 v2.7 P1-1：floor 模式下定仓按抬到下限的止损，挂单风险却按原始计划止损算（少算）。"""
    s = strat({"stop_min_mode": "floor"})
    amount, _ = sized_amount(s, 100.0, 99.5)                                 # 计划止损 0.5%，floor → 0.8%
    t = real_trade("pending", qty=amount, enter_tag=tag(99.5))
    assert abs(s._open_risk([t]) - amount * 0.8) < 1e-9                       # v2.7：amount × 0.5
    assert abs(s._open_risk([t]) - EQUITY * 0.005 * 0.008 / (0.008 + 0.0016 + 0.0016)) < 1e-6
    f = real_trade("filled", qty=amount, enter_tag=tag(99.5))                 # 按下单价成交：止损就是下单时的 99.2
    for policy in ("widen", "exit", "keep"):
        s2 = strat({"stop_min_mode": "floor", "better_fill_policy": policy})
        f2 = real_trade("filled", qty=amount, enter_tag=tag(99.5))
        sp = s2._bind_stop(f2)                                                # floor 不会被当成“成交价更优”而离场
        assert sp is not None and abs(sp - 100 * (1 - 0.0082)) < 1e-9 and not f2.get_custom_data("fw_invalid"), policy
    assert f.get_custom_data("stop_px") is None


def test_partial_fill_that_cannot_be_cancelled_blocks_entries_and_alerts(caplog):
    """审查 v2.7 P0：freqtrade 拒绝撤单（碎仓）或撤单未确认时，部分成交的单子一直挂着，已成交部分没有止损。"""
    q = real_trade("partial", enter_tag=tag(99.0), pair="SOL/USDT:USDT")
    s = strat(open_trades=[q])
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False
    with caplog.at_level(logging.ERROR):
        assert s.check_entry_timeout(q.pair, q, q.orders[0], NOW) is True
        assert s._watch_partial_entries(NOW, [q]) == []
        assert s._watch_partial_entries(NOW + timedelta(seconds=61), [q]) == [q]
        assert s._watch_partial_entries(NOW + timedelta(seconds=90), [q]) == [q]
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and "没有止损" in errs[0].getMessage()               # 告警一次
    assert q.get_custom_data("entry_stuck_since") and q.get_custom_data("partial_since")
    o = q.orders[0]
    o.ft_is_open, o.status, q.amount = False, "canceled", o.filled            # 人工或交易所撤掉后恢复
    assert not s._partial_entry_open(q)
    s._watch_partial_entries(NOW + timedelta(seconds=120), [q])
    assert q.get_custom_data("partial_resolved_at")


def test_partial_fill_alert_survives_the_unfilled_timeout_branch(caplog):
    """审查 v2.8 P1-1：订单超过 unfilledtimeout（3 分钟）后 freqtrade 直接按超时处理、不再调用 check_entry_timeout。
    用 freqtrade 自己的 ft_check_timed_out 走调度，再按 freqtrade 每轮调用 bot_loop_start。"""
    q = real_trade("partial", enter_tag=tag(99.0), pair="SOL/USDT:USDT")
    q.orders[0].order_date = NOW                                              # 下单时间
    s = strat(open_trades=[q])
    s.config = {"unfilledtimeout": {"entry": 3, "exit": 10, "unit": "minutes"}}
    called = []
    orig = s.check_entry_timeout
    s.check_entry_timeout = lambda *a, **k: called.append(1) or orig(*a, **k)
    t150 = NOW + timedelta(seconds=150)                                        # 150 秒时部分成交、撤单被拒
    assert s.ft_check_timed_out(q, q.orders[0], t150) is True and called
    s.bot_loop_start(t150)
    called.clear()
    t240 = NOW + timedelta(seconds=240)                                        # 超过 3 分钟：回调被跳过
    with caplog.at_level(logging.ERROR):
        assert s.ft_check_timed_out(q, q.orders[0], t240) is True and not called
        s.bot_loop_start(t240)                                                 # 独立监测仍然告警
    assert [r for r in caplog.records if "没有止损" in r.getMessage()]
    assert q.get_custom_data("entry_stuck_since")


@pytest.mark.parametrize("engine_stop,rate,expect", [
    (90.1, 98.0, "fw_stop_crossed"),              # 审查 v2.8 P0：首次设置时价格已跌破，引擎还停在兜底价
    (90.1, 99.0, "fw_stop_crossed"),              # 正好到止损
    (99.0, 98.0, None),                           # 引擎已采用止损：交给引擎 / 交易所端止损
    (98.96, 98.0, None),                          # 按价格精度取整后的止损也算已采用
    (90.1, 99.5, None),                           # 还没到止损
])
def test_stop_crossed_before_the_engine_adopts_it_exits(engine_stop, rate, expect):
    s = strat()
    amount, _ = sized_amount(s, 100.0, 99.0)
    t = real_trade("filled", qty=amount, enter_tag=tag(99.0))
    t.adjust_stop_loss(t.open_rate, -0.99, initial=True)
    assert abs(t.stop_loss - 90.1) < 1e-9                                      # 10 倍杠杆的兜底止损
    t.stop_loss = engine_stop
    assert s._bind_stop(t) == 99.0
    if rate <= 99.0:
        assert s.custom_stoploss(t.pair, t, NOW, rate, -0.02, False) is None  # 不再返回会被忽略的 0
    assert s.custom_exit(t.pair, t, NOW, rate, -0.02) == expect
    if expect:
        assert t.get_custom_data("stop_crossed_at")
        assert s.custom_exit(t.pair, t, NOW + timedelta(seconds=5), rate, -0.02) == expect   # 离场失败：下一轮重试


def test_stop_crossed_after_restart_rebinds_and_exits():
    """重启后第一次检查：止损从 enter_tag 重新绑定，若价格已穿过止损立即离场。"""
    s = strat()
    amount, _ = sized_amount(s, 100.0, 99.0)
    t = real_trade("filled", qty=amount, enter_tag=tag(99.0))
    t.adjust_stop_loss(t.open_rate, -0.99, initial=True)
    fresh = strat()
    assert fresh.custom_exit(t.pair, t, NOW, 97.0, -0.03) == "fw_stop_crossed" and t.get_custom_data("stop_px") == 99.0


def test_research_overrides_beat_framework_params_only_in_backtest(monkeypatch):
    """审查 v2.7 P2：策略的 framework_params 覆盖了研究方案的环境变量，方案实际没跑。"""
    monkeypatch.setenv("S03_RESEARCH_OVERRIDES", json.dumps({"stop_min_mode": "floor"}))
    s = strat({"stop_min_mode": "skip"})
    assert s.fw["stop_min_mode"] == "skip"                                    # dry-run / 实盘：忽略
    s.dp.runmode = SimpleNamespace(value="backtest")
    assert s.fw["stop_min_mode"] == "floor"                                   # 回测：研究方案优先
    monkeypatch.setenv("S03_RESEARCH_OVERRIDES", json.dumps({"risk_per_trade": 0.05}))
    with pytest.raises(ValueError, match="risk_per_trade"):
        _ = s.fw
    monkeypatch.delenv("S03_RESEARCH_OVERRIDES")
    import subprocess                                                         # 旧环境变量不再影响任何运行模式
    r = subprocess.run([sys.executable, "-c", "import framework_base as F; print(F.FW_DEFAULTS['stop_min_mode'], "
                        "F.FW_DEFAULTS['better_fill_policy'], F.FW_DEFAULTS['research_entry_delay_bars'])"],
                       capture_output=True, text=True, cwd=ROOT / "user_data" / "strategies", timeout=120,
                       env={**os.environ, "S03_STOP_MIN_MODE": "floor", "S03_BETTER_FILL_POLICY": "exit", "S03_ENTRY_DELAY_BARS": "3"})
    assert r.stdout.split() == ["skip", "widen", "0"], r.stderr


# ------------------------------------------------------------------ v2.10：外部审查 v2.9
def _closed(reason, minutes_ago, profit=-0.01):
    return SimpleNamespace(exit_reason=reason, close_date_utc=NOW - timedelta(minutes=minutes_ago), close_profit=profit)


def test_framework_stop_exits_count_toward_the_stop_guard():
    """审查 v2.9 P1-2：StoplossGuard 只认 freqtrade 自己的止损原因，fw_stop_crossed / fw_invalid_stop 不计数。"""
    six = [_closed("fw_stop_crossed", 30 * i) for i in range(3)] + [_closed("stop_loss", 100 + 30 * i) for i in range(3)]
    s = strat(closed_trades=six)
    assert s._stop_guard_reason(NOW) and "框架离场 3 次" in s._stop_guard_reason(NOW)
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100.0, "GTC", NOW, tag(99.0), "long") is False
    assert strat(closed_trades=six[:5])._stop_guard_reason(NOW) is None                      # 5 次：不触发
    assert strat(closed_trades=six)._stop_guard_reason(NOW + timedelta(days=1, minutes=1)) is None   # 暂停 1 天后解除
    wins = [_closed("fw_stop_crossed", 30 * i, profit=0.01) for i in range(6)]
    assert strat(closed_trades=wins)._stop_guard_reason(NOW) is None                         # 盈利离场不计
    plain = [_closed("stop_loss", 30 * i) for i in range(6)]
    assert strat(closed_trades=plain)._stop_guard_reason(NOW) is None                        # 纯 freqtrade 原因交给 StoplossGuard
    assert F.is_stop_exit("fw_invalid_stop:no_plan") and not F.is_stop_exit("roi")
    assert s.protections[0]["trade_limit"] == 6


def test_initial_risk_is_recorded_when_the_stop_is_bound():
    s = strat()
    amount, _ = sized_amount(s, 100.0, 99.0)
    t = real_trade("filled", qty=amount, enter_tag=tag(99.0))
    s._bind_stop(t)
    assert abs(t.get_custom_data("fw_init_risk") - amount * 1.0) < 1e-9



def test_backtest_writes_initial_risk_records(tmp_path, monkeypatch):
    """审查 v2.10 P1-2：回测导出不带 custom data；框架在回测里把每笔的初始风险写进旁路文件。"""
    log = tmp_path / "risk.jsonl"
    monkeypatch.setenv("S03_RISK_LOG", str(log))
    s = strat({"stop_max_pct": 5.0})
    s.dp.runmode = SimpleNamespace(value="backtest")
    amount, _ = sized_amount(s, 100.0, 96.0)
    t = real_trade("filled", qty=amount, enter_tag=tag(96.0))
    s._bind_stop(t)
    bad = real_trade("filled", qty=amount, enter_tag=tag(99.0), price=98.5)      # 下单 100、跳空成交在止损之下：无效
    bad.open_rate_requested = 100.0
    s._bind_stop(bad)
    recs = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(recs) == 2 and abs(recs[0]["init_risk"] - amount * 4.0) < 1e-9 and recs[0]["stop_max_pct"] == 5.0
    assert recs[1]["invalid"] == "stop_not_below_fill" and abs(recs[1]["init_risk"] - amount * 1.0) < 1e-9   # 按下单时的风险预算
    s.dp.runmode = SimpleNamespace(value="dry_run")                               # 实盘 / dry-run 不写
    s._bind_stop(real_trade("filled", qty=amount, enter_tag=tag(96.0)))
    assert len(log.read_text().splitlines()) == 2



# ------------------------------------------------------------------ v2.13：多空与方向设置
def test_guarded_stop_short_side():
    fw = {**F.FW_DEFAULTS}
    assert F.guarded_stop(100.0, 101.0, fw, True) == 101.0                     # 空单止损在上方
    assert F.guarded_stop(100.0, 99.0, fw, True) is None                       # 空单止损在下方：不合规
    assert F.guarded_stop(100.0, 104.0, fw, True) is None                      # 超过 3%
    assert F.guarded_stop(100.0, 100.5, fw, True) is None                      # 太近、skip 模式
    assert abs(F.guarded_stop(100.0, 100.5, {**fw, "stop_min_mode": "floor"}, True) - 100.8) < 1e-9
    assert F.guarded_stop(100.0, 101.0, fw) is None                            # 多单止损在上方：不合规


def short_sized(s, rate, stop):
    lev = s.leverage("BTC/USDT:USDT", NOW, rate, 10, 75, tag(stop, side="short"), "short")
    stake = s.custom_stake_amount("BTC/USDT:USDT", NOW, rate, 0, 0, 1e9, lev, tag(stop, side="short"), "short")
    return stake * lev / rate, lev


def test_short_sizing_leverage_and_entry_price_mirror_long():
    s = strat()
    amt_s, lev_s = short_sized(s, 100.0, 101.0)
    amt_l, lev_l = sized_amount(s, 100.0, 99.0)
    assert abs(amt_s - amt_l) < 1e-9 and lev_s == lev_l                       # 同样 1% 止损：同样的仓位与杠杆
    assert amt_s * 1.0 <= EQUITY * 0.005 + 1e-9
    assert s.custom_stake_amount("BTC/USDT:USDT", NOW, 100.0, 100, 0, 1e9, 10, tag(99.0), "short") == 0   # 空单止损在下方
    t = tag(101.0, cap=99.9, side="short")                                                   # 空单的 fw_entry_cap 是限价下限
    assert s.custom_entry_price("BTC/USDT:USDT", None, NOW, 99.5, t, "short") == 99.9
    assert s.custom_entry_price("BTC/USDT:USDT", None, NOW, 100.2, t, "short") == 100.2


def test_short_confirm_bind_and_stop_handling():
    s = strat()
    amt, _ = short_sized(s, 100.0, 101.0)
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", amt, 100.0, "GTC", NOW, tag(101.0, side="short"), "short") is True
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", amt, 101.5, "GTC", NOW, tag(101.0, side="short"), "short") is False  # 下单价高于止损
    t = real_trade("filled", qty=amt, enter_tag=tag(101.0, side="short"), is_short=True)
    assert s._bind_stop(t) == 101.0 and abs(t.get_custom_data("fw_init_risk") - amt * 1.0) < 1e-9
    assert s._open_risk([t]) == amt * 1.0
    sl = s.custom_stoploss(t.pair, t, NOW, 100.0, 0.0, False)
    assert sl is not None and abs(sl - 0.01 * 10) < 1e-9                      # 1% × 10 倍杠杆
    better = real_trade("filled", qty=amt, enter_tag=tag(101.0, side="short"), is_short=True, price=100.5)   # 空单成交价更高 = 更优
    better.open_rate_requested = 100.0
    sp = s._bind_stop(better)
    assert sp is not None and (sp - 100.5) / 100.5 * 100 >= 0.8 and better.get_custom_data("stop_widened")
    gap = real_trade("filled", qty=amt, enter_tag=tag(101.0, side="short"), is_short=True, price=101.5)      # 跳空成交在止损之上
    gap.open_rate_requested = 100.0
    assert s._bind_stop(gap) is None and gap.get_custom_data("fw_invalid") == "stop_not_above_fill"


@pytest.mark.parametrize("engine_stop,rate,expect", [
    (109.9, 102.0, "fw_stop_crossed"),            # 空单：价格涨过止损，引擎还停在上方很远的兜底价
    (101.0, 102.0, None),                         # 引擎已采用止损
    (109.9, 100.5, None),                         # 还没到止损
])
def test_short_stop_crossed(engine_stop, rate, expect):
    s = strat()
    amt, _ = short_sized(s, 100.0, 101.0)
    t = real_trade("filled", qty=amt, enter_tag=tag(101.0, side="short"), is_short=True)
    t.stop_loss = engine_stop
    assert s._bind_stop(t) == 101.0
    if rate >= 101.0:
        assert s.custom_stoploss(t.pair, t, NOW, rate, -0.02, False) is None
    assert s.custom_exit(t.pair, t, NOW, rate, -0.02) == expect


def test_short_take_profit_triggers_below_entry():
    s = strat()
    S.take_profit_legs = lambda self, tr: [(2.0, 0.5, "tp_2R")]
    try:
        amt, _ = short_sized(s, 100.0, 101.0)
        t = real_trade("filled", qty=amt, enter_tag=tag(101.0, side="short"), is_short=True)
        args = (0, 0, 1e9, 98.0, 98.0, 0.0, 0.0)
        assert s.adjust_trade_position(t, NOW, 98.5, 0.0, *args[1:]) is None           # 1.5R：还没到
        out = s.adjust_trade_position(t, NOW, 97.9, 0.0, *args[1:])                     # 2.1R：平一半
        assert out is not None and out[1] == "tp_2R" and abs(out[0] + t.stake_amount * 0.5) < 1e-6
    finally:
        S.take_profit_legs = F.FrameworkStrategy.take_profit_legs


def test_correlation_is_direction_aware():
    d1 = pd.DataFrame({"date": pd.date_range("2026-09-01", periods=40, freq="D", tz="UTC"),
                       "close": [100 * 1.01 ** i * (1 + 0.02 * (i % 2)) for i in range(40)]})
    held_long = real_trade("filled", enter_tag=tag(99.0), stop_px=99.0, pair="ETH/USDT:USDT")
    s = strat(open_trades=[held_long])
    s.dp = SimpleNamespace(get_pair_dataframe=lambda p, tf: d1, runmode=SimpleNamespace(value="dry_run"))
    assert s._corr_ok("BTC/USDT:USDT", [held_long], is_short=False) is False   # 同向、相关 = 1：同一笔风险
    assert s._corr_ok("BTC/USDT:USDT", [held_long], is_short=True) is True     # 反向：对冲，不拒绝（风险仍相加）


@pytest.mark.parametrize("config,side,pair,ok", [
    ({}, "short", "BTC/USDT:USDT", True),                                                   # 默认 both
    ({"fw_trade_direction": "long"}, "short", "BTC/USDT:USDT", False),
    ({"fw_trade_direction": "long"}, "long", "BTC/USDT:USDT", True),
    ({"fw_trade_direction": "short"}, "long", "BTC/USDT:USDT", False),
    ({"fw_trade_direction": "both", "fw_pair_directions": {"HYPE/USDT:USDT": "long"}}, "short", "HYPE/USDT:USDT", False),
    ({"fw_trade_direction": "both", "fw_pair_directions": {"HYPE/USDT:USDT": "long"}}, "short", "BTC/USDT:USDT", True),
    ({"fw_trade_direction": "sideways"}, "long", "BTC/USDT:USDT", False),                   # 无效设置：拒绝（出错即拒绝）
])
def test_direction_setting_gates_entries(config, side, pair, ok):
    s = strat(config=config)
    stop = 101.0 if side == "short" else 99.0
    assert s.confirm_trade_entry(pair, "limit", 1.0, 100.0, "GTC", NOW, tag(stop, side=side), side) is ok


def test_direction_setting_clears_disallowed_signals_and_is_validated_at_start():
    df = pd.DataFrame({"date": pd.date_range(NOW, periods=3, freq="15min"), "close": [100.0] * 3,
                       "enter_long": [1, 0, 0], "enter_short": [0, 1, 0], "fw_stop": [99.0, 101.0, None]})
    out = strat(config={"fw_trade_direction": "long"}).advise_entry(df.copy(), {"pair": "BTC/USDT:USDT"})
    assert out["enter_long"].tolist() == [1, 0, 0] and out["enter_short"].tolist() == [0, 0, 0]
    out = strat(config={"fw_trade_direction": "both"}).advise_entry(df.copy(), {"pair": "BTC/USDT:USDT"})
    assert out["enter_short"].tolist() == [0, 1, 0] and F.decode_plan(out["enter_tag"][1])["stop"] == 101.0
    with pytest.raises(ValueError, match="fw_trade_direction"):
        strat(config={"fw_pair_directions": {"BTC/USDT:USDT": "up"}}).bot_start()
    strat(config={"fw_trade_direction": "short"}).bot_start()
    assert F.FrameworkStrategy.can_short is True



def test_delay1_shifts_short_signals_too(monkeypatch):
    monkeypatch.setenv("S03_RESEARCH_OVERRIDES", json.dumps({"research_entry_delay_bars": 1}))
    s = strat()
    s.dp.runmode = SimpleNamespace(value="backtest")
    df = pd.DataFrame({"date": pd.date_range(NOW, periods=3, freq="15min"), "close": [100.0] * 3,
                       "enter_long": [0, 0, 0], "enter_short": [1, 0, 0], "fw_stop": [101.0, None, None]})
    out = s.advise_entry(df, {"pair": "BTC/USDT:USDT"})
    assert out["enter_short"].tolist() == [0, 1, 0] and F.decode_plan(out["enter_tag"][1])["stop"] == 101.0
