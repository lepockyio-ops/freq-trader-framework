"""freqtrade 版行情监控进程（独立于 freqtrade，不需要交易所密钥，不消耗 LLM token）。

  python monitor/run_monitor.py alerts -c monitor/monitor.toml

数据来源（全部只读）：
  - Binance USDⓈ-M WebSocket（/market 路径）：kline_1m、kline_15m、markPrice@1s（含资金费率）
  - Binance REST（启动时预热）：最近 1,500 根 1 分钟 K 线、20 根 15 分钟 K 线、近 30 天资金费率
  - OKX 公开 ticker（每 60 秒）：参考价偏离
  - freqtrade REST API GET /api/v1/status（每 10 秒）：持仓、初始止损、交易所端止损单是否在场

告警写入 monitor/var/alerts.jsonl；符合限流的再交给 OpenClaw 分析。
freqtrade 不依赖本进程：本进程挂掉，交易与交易所端止损照常运行。
"""
from __future__ import annotations

import json
import os
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import requests

from alerts_core import SEV_CRIT, SEV_INFO, SEV_WARN, Alert, AlertConfig, AlertEngine, PartialEntryView, PositionView
from openclaw_notify import OpenClawConfig, OpenClawNotifier
from sdnotify import notify

FAPI = "https://fapi.binance.com"
OKX = "https://www.okx.com/api/v5/market/ticker"


@dataclass(frozen=True)
class MonitorConfig:
    pairs: tuple[str, ...] = ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "HYPE/USDT:USDT")
    ws_base: str = "wss://fstream.binance.com/market/stream"   # Binance 2026 年起的新路径；以官方公告为准
    rest_base: str = FAPI
    ft_api_url: str = "http://127.0.0.1:8080"                 # freqtrade api_server 地址
    require_exchange_stop: bool = True                        # 检查交易所端止损单；dry-run 下请先确认 freqtrade 会显示模拟止损单
    journal_path: str = "monitor/var/alerts.jsonl"
    poll_s: int = 10
    ref_every_s: int = 60
    webhook_port: int = 8766                                  # 接收 freqtrade 自带 webhook 的本机端口（0 = 不启用）
    alerts: AlertConfig = field(default_factory=AlertConfig)
    openclaw: OpenClawConfig = field(default_factory=lambda: OpenClawConfig(venue="Binance USDⓈ-M"))


def load_config(path: str | None) -> MonitorConfig:
    raw = tomllib.loads(Path(path).read_text("utf-8")) if path and Path(path).exists() else {}

    def tup(d):
        return {k: tuple(v) if isinstance(v, list) else v for k, v in (d or {}).items()}

    base = tup({k: v for k, v in raw.items() if not isinstance(v, dict)})
    return MonitorConfig(**base, alerts=AlertConfig(**tup(raw.get("alerts"))),
                         openclaw=OpenClawConfig(**{"venue": "Binance USDⓈ-M", **tup(raw.get("openclaw"))}))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def symbol(pair: str) -> str:
    """BTC/USDT:USDT → BTCUSDT"""
    base, rest = pair.split("/")
    return base + rest.split(":")[0]


def coin_of(pair: str) -> str:
    return pair.split("/")[0]


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def write(self, kind: str, **fields) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, **fields}
        with self.lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text("utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------------- freqtrade API（只读）
class FtClient:
    """只用 GET /api/v1/status 与 GET /api/v1/show_config；以及给白名单代理用的 POST /api/v1/stopentry。
    用户名和密码从环境变量 FT_API_USER / FT_API_PASS 读取，绝不交给 OpenClaw。"""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        user, pw = os.environ.get("FT_API_USER"), os.environ.get("FT_API_PASS")
        self.auth = (user, pw) if user and pw else None
        self.s = requests.Session()

    def get(self, path: str):
        r = self.s.get(f"{self.url}/api/v1/{path}", auth=self.auth, timeout=10)
        r.raise_for_status()
        return r.json()

    def stopentry(self):
        r = self.s.post(f"{self.url}/api/v1/stopentry", auth=self.auth, timeout=10)
        r.raise_for_status()
        return r.json()


def positions_from_ft(trades: list[dict], require_exchange_stop: bool = True) -> list[PositionView]:
    """把 freqtrade /status 的未平仓交易转成只读持仓视图（按 is_short 保留方向）。"""
    out = []
    for t in trades:
        if not t.get("is_open"):
            continue
        if t.get("amount") is not None and float(t.get("amount") or 0) <= 0:
            continue                                    # 入场单还挂着、没有持仓：不是“缺止损”（v2.9，避免普通挂单误报）
        entry = t.get("open_rate")
        init_stop = t.get("stop_loss_abs")              # 框架止损按价格设定且不移动；initial_* 是兜底的 −99%
        cur_stop = init_stop
        if not entry or not init_stop:
            continue
        has_stop = any(o.get("ft_order_side") == "stoploss" and o.get("is_open") for o in t.get("orders") or [])
        ts = (t.get("open_fill_timestamp") or t.get("open_timestamp") or time.time() * 1000) / 1000
        out.append(PositionView(coin_of(t["pair"]), float(entry), float(init_stop), float(cur_stop),
                                has_stop or not require_exchange_stop, ts, bool(t.get("is_short"))))
    return out


def partial_entries_from_ft(trades: list[dict]) -> list[PartialEntryView]:
    """有部分成交、仍挂着的入场单（多头入场 buy、空头入场 sell）。"""
    out = []
    for t in trades:
        if not t.get("is_open"):
            continue
        for o in t.get("orders") or []:
            if o.get("ft_order_side") == ("sell" if t.get("is_short") else "buy") and o.get("is_open") and float(o.get("filled") or 0) > 0:
                ts = (o.get("order_timestamp") or time.time() * 1000) / 1000
                out.append(PartialEntryView(coin_of(t["pair"]), float(o.get("filled") or 0), ts))
    return out


# ---------------------------------------------------------------------- 监控主体
class BinanceMonitor:
    def __init__(self, cfg: MonitorConfig):
        self.cfg = cfg
        self.coins = [coin_of(p) for p in cfg.pairs]
        self.sym2coin = {symbol(p).lower(): coin_of(p) for p in cfg.pairs}
        self.engine = AlertEngine(cfg.alerts)
        self.journal = Journal(cfg.journal_path)
        self.notifier = OpenClawNotifier(cfg.openclaw, log=lambda d: self.journal.write("openclaw", **d))
        self.ft = FtClient(cfg.ft_api_url)
        self.lock = threading.Lock()
        self.mark: dict[str, dict] = {}
        self.next_funding: dict[str, int] = {}
        self.ws = None

    # ------------------------------------------------------------------ 告警出口
    def handle(self, alerts) -> None:
        for a in alerts:
            to_llm = self.engine.allow_llm(a)
            self.journal.write("alert", **a.to_dict(), to_openclaw=to_llm and self.cfg.openclaw.enabled)
            if to_llm:
                st = self.engine.stats.get(a.coin)
                m = self.mark.get(a.coin, {})
                self.notifier.send(a, {"last_px": st.last_price if st else None, "mark_px": m.get("p"),
                                       "index_px": m.get("i"), "funding_8h": m.get("r")})

    # ------------------------------------------------------------------ freqtrade 事件推送（webhook）
    CRIT_EXITS = {"liquidation", "emergency_exit", "fw_stop_crossed"}        # fw_stop_crossed：价格穿过止损时引擎还没采用止损
    CRIT_EXIT_PREFIXES = ("fw_invalid_stop",)                                 # 成交后止损无效，立即离场
    WARN_EXITS = {"stop_loss", "stoploss_on_exchange", "trailing_stop_loss", "force_exit"}

    def on_ft_event(self, d: dict) -> Alert | None:
        """freqtrade webhook（format = json）推来的事件。全部写日志；只有 warning / critical 才可能转给 OpenClaw。
        成交事件不做冷却：每一笔都记录。"""
        typ = str(d.get("type", ""))
        if typ not in ("entry_fill", "exit_fill", "status"):
            return None
        coin = str(d.get("pair", "SYSTEM")).split("/")[0] or "SYSTEM"
        reason = str(d.get("exit_reason") or "")
        if typ == "exit_fill":
            crit = reason in self.CRIT_EXITS or reason.startswith(self.CRIT_EXIT_PREFIXES)
            sev = SEV_CRIT if crit else SEV_WARN if reason in self.WARN_EXITS else SEV_INFO
            detail = f"平仓成交：{reason or '未知原因'}，盈亏 {d.get('profit_amount')}"
            value = _num(d.get("profit_amount"))
        elif typ == "entry_fill":
            sev, detail, value = SEV_INFO, f"入场成交：价格 {d.get('open_rate')}", _num(d.get("open_rate"))
        else:
            txt = str(d.get("status", ""))
            sev = SEV_WARN if any(k in txt.lower() for k in ("error", "warning", "stopped", "liquidation")) else SEV_INFO
            detail, value = f"freqtrade 状态：{txt[:200]}", None
        a = Alert(coin, f"ft_{typ}" + (f"_{reason}" if reason else ""), sev, value, None, time.time(), detail,
                  {k: d.get(k) for k in ("pair", "exit_reason", "profit_amount", "close_rate", "open_rate")})
        with self.lock:
            to_llm = sev != SEV_INFO and self.engine.allow_llm(a)
            self.journal.write("alert", **a.to_dict(), to_openclaw=to_llm and self.cfg.openclaw.enabled)
            if to_llm:
                self.notifier.send(a)
        return a

    def start_webhook_server(self) -> None:
        if not self.cfg.webhook_port:
            return
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        mon = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/ft-webhook":
                    self.send_response(404)
                    self.end_headers()
                    return
                n = min(int(self.headers.get("Content-Length") or 0), 65536)
                try:
                    mon.on_ft_event(json.loads(self.rfile.read(n) or b"{}"))
                    self.send_response(200)
                except ValueError:
                    self.send_response(400)
                self.end_headers()

            def log_message(self, *a):
                return

        srv = ThreadingHTTPServer(("127.0.0.1", self.cfg.webhook_port), H)
        threading.Thread(target=srv.serve_forever, name="ft-webhook", daemon=True).start()

    # ------------------------------------------------------------------ 消息解析（便于测试）
    def on_message(self, raw: str) -> None:
        msg = json.loads(raw)
        d = msg.get("data", msg)
        ev = d.get("e")
        coin = self.sym2coin.get(str(d.get("s", "")).lower())
        if not coin:
            return
        with self.lock:
            if ev == "kline":
                k = d["k"]
                close = float(k["c"])
                self.engine.on_price(coin, close)
                if not k.get("x"):                                   # 只用已收盘的 K 线
                    return
                t = int(k["t"]) / 1000
                if k["i"] == "1m":
                    self.handle(self.engine.on_bar_1m(coin, t, close))
                elif k["i"] == "15m":
                    self.handle(self.engine.on_bar_15m(coin, t, float(k["h"]), float(k["l"]), close))
            elif ev == "markPriceUpdate":
                prev_msg = self.mark.get(coin)
                self.mark[coin] = d
                self.engine.on_price(coin, float(d["p"]))
                nft = int(d.get("T") or 0)
                prev = self.next_funding.get(coin)
                self.next_funding[coin] = nft
                if prev is not None and nft > prev and prev_msg:     # 一期资金费刚结算：记录结算前最后一次的费率
                    self.handle(self.engine.on_funding(coin, float(prev_msg.get("r", 0))))

    # ------------------------------------------------------------------ 预热
    def seed(self) -> None:
        for p in self.cfg.pairs:
            c, sym = coin_of(p), symbol(p)
            try:
                fr = requests.get(f"{self.cfg.rest_base}/fapi/v1/fundingRate",
                                  params={"symbol": sym, "limit": 90}, timeout=10).json()
                self.engine.seed_funding(c, [x["fundingRate"] for x in fr])
                k1 = requests.get(f"{self.cfg.rest_base}/fapi/v1/klines",
                                  params={"symbol": sym, "interval": "1m", "limit": 1500}, timeout=15).json()
                for b in k1[:-1]:
                    self.engine.on_bar_1m(c, b[0] / 1000, float(b[4]))
                k15 = requests.get(f"{self.cfg.rest_base}/fapi/v1/klines",
                                   params={"symbol": sym, "interval": "15m", "limit": 20}, timeout=15).json()
                for b in k15[:-1]:
                    self.engine.on_bar_15m(c, b[0] / 1000, float(b[2]), float(b[3]), float(b[4]))
            except Exception as e:
                self.journal.write("error", where="seed", coin=c, error=repr(e))
        self.engine._last_fire.clear()

    # ------------------------------------------------------------------ WebSocket（自动重连）
    def ws_url(self) -> str:
        streams = []
        for p in self.cfg.pairs:
            s = symbol(p).lower()
            streams += [f"{s}@kline_1m", f"{s}@kline_15m", f"{s}@markPrice@1s"]
        return f"{self.cfg.ws_base}?streams=" + "/".join(streams)

    def ws_loop(self) -> None:
        import websocket                                             # websocket-client

        backoff = 1
        while True:
            self.ws = websocket.WebSocketApp(self.ws_url(), on_message=lambda ws, m: self.on_message(m),
                                             on_error=lambda ws, e: self.journal.write("ws_error", error=repr(e)))
            self.journal.write("ws_connect")
            t0 = time.time()
            self.ws.run_forever(ping_interval=60, ping_timeout=20)  # Binance 每 24 小时会主动断开
            backoff = 1 if time.time() - t0 > 60 else min(backoff * 2, 60)
            time.sleep(backoff)

    def reconnect(self) -> None:
        try:
            if self.ws:
                self.ws.close()                                      # ws_loop 会自动重连
        except Exception:
            pass

    # ------------------------------------------------------------------ 主循环
    def run_forever(self) -> None:
        self.seed()
        self.notifier.start()
        self.start_webhook_server()
        threading.Thread(target=self.ws_loop, name="binance-ws", daemon=True).start()
        notify("READY=1")
        last_ref, stale_since = 0.0, None
        while True:
            time.sleep(self.cfg.poll_s)
            notify("WATCHDOG=1")
            now = time.time()
            try:
                ft_ok = True
                try:
                    trades = self.ft.get("status")
                    pos = positions_from_ft(trades, self.cfg.require_exchange_stop)
                    partial = partial_entries_from_ft(trades)
                except Exception as e:
                    ft_ok = False                            # 读不到 ≠ 没有异常：不调用持仓检查，保留计时（v2.10）
                    self.journal.write("error", where="ft_status", error=type(e).__name__)
                with self.lock:
                    self.handle(self.engine.check_ft_status(ft_ok, now))
                    if ft_ok:
                        self.handle(self.engine.check_positions(pos, now))
                        self.handle(self.engine.check_partial_entries(partial, now))
                    self.handle(self.engine.check_feed(self.coins, now))
                if now - last_ref >= self.cfg.ref_every_s:
                    last_ref = now
                    for c in self.coins:
                        try:
                            r = requests.get(OKX, params={"instId": f"{c}-USDT-SWAP"}, timeout=10)
                            ref = float(r.json()["data"][0]["last"])
                        except Exception:
                            continue
                        with self.lock:
                            self.handle(self.engine.on_ref_price(c, ref, now))
                lus = [self.engine.stats[c].last_update for c in self.coins if c in self.engine.stats]
                if not lus or min(x or 0 for x in lus) < now - 60:
                    stale_since = stale_since or now
                    if now - stale_since > 60:
                        self.journal.write("ws_force_reconnect")
                        self.reconnect()
                        stale_since = None
                else:
                    stale_since = None
            except Exception as e:
                self.journal.write("error", where="monitor_loop", error=repr(e))
