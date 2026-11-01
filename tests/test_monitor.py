"""freqtrade 版监控测试：Binance 消息解析、freqtrade 持仓转换、配置加载。核心阈值逻辑与 HL 版共用（见 HL 版 tests/test_alerts.py）。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor"))

from binance_monitor import BinanceMonitor, MonitorConfig, load_config, positions_from_ft, symbol  # noqa: E402


def _kline(sym, iv, t, o, h, l, c, closed):
    return json.dumps({"stream": f"{sym.lower()}@kline_{iv}", "data": {"e": "kline", "s": sym, "k": {
        "t": t, "i": iv, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "x": closed}}})


def test_symbol():
    assert symbol("BTC/USDT:USDT") == "BTCUSDT"


def test_only_closed_klines_count(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl")))
    m.on_message(_kline("BTCUSDT", "1m", 0, 1, 1, 1, 100, False))
    assert m.engine.stats["BTC"].last_1m_open_ts is None and m.engine.stats["BTC"].last_price == 100
    m.on_message(_kline("BTCUSDT", "1m", 0, 1, 1, 1, 100, True))
    assert m.engine.stats["BTC"].last_1m_open_ts == 0


def test_shock_alert_written_to_journal(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl")))
    px = 100.0
    for i in range(300):
        px *= 1.0005 if i % 2 else 0.9995
        m.on_message(_kline("ETHUSDT", "1m", i * 60_000, px, px, px, px, True))
    m.on_message(_kline("ETHUSDT", "1m", 300 * 60_000, px, px, px, px * 1.01, True))
    recs = [r for r in m.journal.read() if r["kind"] == "alert"]
    assert recs and recs[0]["rule"] == "shock_1m" and recs[0]["to_openclaw"] is False   # OpenClaw 默认关闭


def test_funding_recorded_on_settlement(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl")))
    m.engine.seed_funding("SOL", [0.0001] * 90)

    def mp(r, T):
        return json.dumps({"data": {"e": "markPriceUpdate", "s": "SOLUSDT", "p": "100", "i": "100", "r": str(r), "T": T}})

    m.on_message(mp(0.0001, 1000))
    m.on_message(mp(0.002, 1000))      # 结算前的预测费率
    m.on_message(mp(0.0001, 2000))     # 结算：应记录 0.002（上一条）
    assert list(m.engine.stats["SOL"].funding_hist)[-1] == 0.002
    assert any(r["rule"] == "funding_extreme" for r in m.journal.read() if r["kind"] == "alert")


def test_positions_from_freqtrade_status():
    trades = [{"pair": "SOL/USDT:USDT", "is_open": True, "is_short": False, "open_rate": 100.0,
               "initial_stop_loss_abs": 97.2, "stop_loss_abs": 97.2, "open_fill_timestamp": 1_800_000_000_000,
               "orders": [{"ft_order_side": "buy", "is_open": False},
                          {"ft_order_side": "stoploss", "is_open": True}]},
              {"pair": "BTC/USDT:USDT", "is_open": True, "is_short": False, "open_rate": 80000.0,
               "initial_stop_loss_abs": 78000.0, "stop_loss_abs": 78000.0, "orders": []}]
    pos = positions_from_ft(trades)
    assert [(p.coin, p.has_exchange_stop) for p in pos] == [("SOL", True), ("BTC", False)]
    assert positions_from_ft(trades, require_exchange_stop=False)[1].has_exchange_stop


def test_pending_entries_are_not_stop_missing_but_stuck_partials_are_critical():
    """审查 v2.8 P2-1：挂着的入场单（数量 0、带兜底止损价）被当成“有持仓缺止损”误报；部分成交卡住的单子没有告警。"""
    from alerts_core import AlertConfig, AlertEngine
    from binance_monitor import partial_entries_from_ft

    pending = {"pair": "ETH/USDT:USDT", "is_open": True, "is_short": False, "open_rate": 3000.0, "amount": 0.0,
               "stop_loss_abs": 30.0, "orders": [{"ft_order_side": "buy", "is_open": True, "filled": 0.0}]}
    partial = {"pair": "SOL/USDT:USDT", "is_open": True, "is_short": False, "open_rate": 100.0, "amount": 0.0,
               "stop_loss_abs": 1.0, "orders": [{"ft_order_side": "buy", "is_open": True, "filled": 0.4,
                                                 "order_timestamp": 1_800_000_000_000}]}
    assert positions_from_ft([pending, partial]) == []
    ents = partial_entries_from_ft([pending, partial])
    assert [(e.coin, e.filled) for e in ents] == [("SOL", 0.4)]
    eng = AlertEngine(AlertConfig())
    assert eng.check_partial_entries(ents, 1000.0) == []
    a = eng.check_partial_entries(ents, 1061.0)
    assert len(a) == 1 and a[0].rule == "entry_partial_stuck" and a[0].severity == "critical"
    assert eng.check_partial_entries([], 1100.0) == [] and eng.stats["SOL"].partial_since is None   # 解决后复位


def test_stop_crossed_exit_is_critical(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl"), webhook_port=0))
    for reason in ("fw_stop_crossed", "fw_invalid_stop:risk_over_budget"):
        a = m.on_ft_event({"type": "exit_fill", "pair": "SOL/USDT:USDT", "exit_reason": reason, "profit_amount": -30})
        assert a.severity == "critical", reason


def test_example_config_loads():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(os.path.join(root, "monitor", "monitor.example.toml"))
    assert cfg.alerts.funding_abs_floor == 0.0003 and cfg.openclaw.enabled is False
    assert cfg.openclaw.venue.startswith("Binance") and len(cfg.pairs) == 5


def test_freqtrade_webhook_events(tmp_path):
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl"), webhook_port=0))
    a1 = m.on_ft_event({"type": "entry_fill", "pair": "SOL/USDT:USDT", "open_rate": "100.1"})
    a2 = m.on_ft_event({"type": "exit_fill", "pair": "SOL/USDT:USDT", "exit_reason": "stoploss_on_exchange",
                        "profit_amount": "-48.2"})
    a3 = m.on_ft_event({"type": "exit_fill", "pair": "BTC/USDT:USDT", "exit_reason": "liquidation", "profit_amount": "-90"})
    assert (a1.severity, a2.severity, a3.severity) == ("info", "warning", "critical")
    assert m.on_ft_event({"type": "entry"}) is None
    recs = [r for r in m.journal.read() if r["kind"] == "alert"]
    assert [r["rule"] for r in recs] == ["ft_entry_fill", "ft_exit_fill_stoploss_on_exchange", "ft_exit_fill_liquidation"]
    assert all(r["to_openclaw"] is False for r in recs)        # OpenClaw 默认关闭


def test_webhook_server_roundtrip(tmp_path):
    import socket
    import urllib.request

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    m = BinanceMonitor(MonitorConfig(journal_path=str(tmp_path / "a.jsonl"), webhook_port=port))
    m.start_webhook_server()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/ft-webhook", method="POST",
                                 data=json.dumps({"type": "status", "status": "Error: exchange timeout"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    rec = [r for r in m.journal.read() if r["kind"] == "alert"][0]
    assert rec["rule"] == "ft_status" and rec["severity"] == "warning"


def test_status_read_failures_keep_timers_and_alert():
    """审查 v2.9 P2-1：读不到 /status 时传空列表，部分成交与缺止损的计时被清零，215 秒的序列没有告警。"""
    from alerts_core import AlertConfig, AlertEngine, PartialEntryView

    eng = AlertEngine(AlertConfig())
    ent = [PartialEntryView("SOL", 0.4, 0.0)]
    got = []
    for t in range(0, 220, 5):
        ok = (t // 5) % 2 == 0                                        # 间歇读取失败
        got += eng.check_ft_status(ok, float(t))
        if ok:
            got += eng.check_partial_entries(ent, float(t))
    assert any(a.rule == "entry_partial_stuck" for a in got)
    eng2 = AlertEngine(AlertConfig())
    eng2.check_partial_entries(ent, 0.0)
    alerts = [a for t in range(5, 130, 5) for a in eng2.check_ft_status(False, float(t))]   # 一直读不到
    stale = [a for a in alerts if a.rule == "ft_status_stale"]
    assert stale and stale[0].severity == "critical" and eng2.stats["SOL"].partial_since == 0.0
    eng3 = AlertEngine(AlertConfig())
    w = [a for t in range(0, 130, 5) for a in eng3.check_ft_status(False, float(t))]
    assert w and w[0].severity == "warning"


def test_monitor_handles_short_positions():
    """v2.13：空单持仓也要检查缺止损与接近止损；空单的入场单是 sell。"""
    from alerts_core import AlertConfig, AlertEngine
    from binance_monitor import partial_entries_from_ft

    short = {"pair": "SOL/USDT:USDT", "is_open": True, "is_short": True, "open_rate": 100.0, "amount": 1.0,
             "stop_loss_abs": 101.0, "orders": [{"ft_order_side": "sell", "is_open": False, "filled": 1.0}]}
    pos = positions_from_ft([short])
    assert len(pos) == 1 and pos[0].is_short and not pos[0].has_exchange_stop
    eng = AlertEngine(AlertConfig())
    st = eng._st("SOL")
    st.last_price = 100.8                                         # 距离止损 0.2R（空单：价格涨向止损）
    alerts = eng.check_positions(pos, 1000.0)
    near = [a for a in alerts if a.rule == "near_stop"]
    assert near and abs(near[0].value - 0.2) < 1e-9
    st.last_price = 99.0                                          # 价格下跌：远离止损
    assert not [a for a in eng.check_positions(pos, 1001.0) if a.rule == "near_stop"]
    stuck = {**short, "amount": 0.0, "orders": [{"ft_order_side": "sell", "is_open": True, "filled": 0.4}]}
    assert [(e.coin, e.filled) for e in partial_entries_from_ft([stuck])] == [("SOL", 0.4)]
