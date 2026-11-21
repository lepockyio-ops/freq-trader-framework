"""资产类别接口（v2.14）：让同一个框架交易“不是 24/7 的标的”，例如交易所的美股永续合约（Binance USDⓈ-M TradFi 永续，
TSLA/USDT:USDT 这类）。只依赖标准库，框架与 doctor 共用。

配置（freqtrade 配置文件，用户设定；默认 us_equity 的 pairs 为空 = 不启用）：
  "fw_asset_classes": {
    "us_equity": {
      "pairs": ["TSLA/USDT:USDT"],            # 属于这一类的交易对（还要自己加进 pair_whitelist）
      "calendar": "nyse",                     # 交易日历：24x7 / nyse / 用 fw_sessions.register_calendar 注册的名字
      "calendar_params": {"session": "regular", "extra_closed_dates": [], "extra_early_closes": []},
      "entry_after_open_min": 15,             # 开盘后多少分钟内不新开仓（示例值）
      "entry_before_close_min": 15,           # 收盘前多少分钟内不新开仓（示例值）
      "flat_before_close_min": 0,             # >0：每天收盘前这么多分钟平掉持仓（0 = 不做）
      "flat_before_gap": false,               # true：下一个交易日在 2 天以上之后（周末、节假日）时，收盘前平仓
      "flat_before_gap_min": 30,              # flat_before_gap 的提前量（分钟，示例值）
      "events_file": "",                      # 个股事件（财报、拆股、分红等）JSON；格式见 load_equity_events
      "events_required": true,                # 实盘 / dry-run：事件文件缺失、损坏、过期时拒绝新开仓
      "events_max_age_days": 3,
      "event_before_min": 1440, "event_after_min": 60,   # 事件前后不新开仓（示例值）
      "flat_before_event": true,              # 持仓遇到 event_before_min 以内的个股事件：提前平仓
      "params": {"risk_per_trade": 0.0025}    # 覆盖框架参数：只能更严格（见 STRICTER），放宽直接报错
    }
  }

不在任何类别里的交易对按加密货币处理（24x7，不做时段与个股事件检查）。交易所标为 TradFi（股票、商品）的合约必须
在某个类别里声明，否则拒绝入场（fail-closed，见 is_tradfi_market）。
"""
from __future__ import annotations

import json
import os
from fnmatch import fnmatchcase
from datetime import datetime, timedelta, timezone

from fw_sessions import make_calendar

UTC = timezone.utc

# 类别可以覆盖的框架参数，以及“更严格”的方向：le = 只能调小，ge = 只能调大
STRICTER = {
    "risk_per_trade": "le",
    "max_position_leverage": "le",
    "stop_max_pct": "le",
    "max_hold_days": "le",
    "stop_min_pct": "ge",
    "liq_to_stop_multiple": "ge",
    "round_trip_cost": "ge",
    "funding_est": "ge",
}

CLASS_DEFAULTS = {
    "pairs": [],
    "calendar": "24x7",
    "calendar_params": {},
    "entry_after_open_min": 0,
    "entry_before_close_min": 0,
    "flat_before_close_min": 0,
    "flat_before_gap": False,
    "flat_before_gap_min": 30,
    "events_file": "",
    "events_required": True,
    "events_max_age_days": 3,
    "event_before_min": 1440,
    "event_after_min": 60,
    "flat_before_event": True,
    "params": {},
}

# 交易所把股票 / 商品等标成 TradFi 时用的原始字段（ccxt market["info"]）与取值。写法是通配模式（fnmatch，不区分大小写），
# 以 "!" 开头表示“字段存在且不匹配”。可以用配置 fw_tradfi_markers 整个替换。
#   - OKX：instCategory 1 = 加密货币，3 = 股票，4 = 商品（已核验：2026-11-21 读 OKX 公开 instruments，1 有 304 个、3 有 233 个、4 有 9 个永续）；
#     不是 1 的一律按 TradFi；
#   - Hyperliquid：HIP-3（第三方部署的永续市场，例如 xyz 的 XYZ-TSLA/USDC:USDC）带 hip3 = true（已核验）；HIP-3 里也可能有加密货币，
#     一律要求声明（它们的预言机、保证金和休市规则由部署方决定）；
#   - Binance：contractType 含 TRADIFI（据二手资料为 TRADIFI_PERPETUAL，来源方观点；本环境访问 Binance API 返回 451，未直接核验）；
#   - underlyingType 为 EQUITY / STOCK / COMMODITY / TRADFI：模型推断的兜底。
TRADFI_MARKERS = {
    "instCategory": ("!1",),
    "hip3": ("TRUE",),
    "contractType": ("*TRADIFI*",),
    "underlyingType": ("EQUITY", "STOCK", "COMMODITY", "TRADFI"),
}


def symbol_of(pair: str) -> str:
    return str(pair).split("/")[0].upper()


def symbol_aliases(pair: str) -> set:
    """个股事件匹配用的代码：交易对的基础币，以及去掉 HIP-3 部署方前缀后的代码（XYZ-TSLA → TSLA）。"""
    b = symbol_of(pair)
    return {b, b.split("-")[-1]}


def merge_params(base: dict, params: dict, cls_name: str = "") -> dict:
    """把类别参数并进框架参数；只允许更严格，否则 ValueError。"""
    out = dict(base)
    for k, v in (params or {}).items():
        if k not in STRICTER:
            raise ValueError(f"资产类别 {cls_name}：参数 {k} 不能按类别覆盖；可覆盖：{sorted(STRICTER)}")
        v = float(v)
        b = float(base[k])
        if (STRICTER[k] == "le" and v > b + 1e-12) or (STRICTER[k] == "ge" and v < b - 1e-12):
            raise ValueError(f"资产类别 {cls_name}：{k}={v} 比框架设置 {b} 更宽松（类别参数只能更严格）")
        out[k] = v
    if out["stop_min_pct"] > out["stop_max_pct"]:
        raise ValueError(f"资产类别 {cls_name}：stop_min_pct {out['stop_min_pct']} > stop_max_pct {out['stop_max_pct']}")
    return out


def parse_classes(config: dict) -> dict:
    """配置 → {类别名: 完整设置}，并做结构校验（日历名、取值、交易对不能属于两个类别）。出错 ValueError。"""
    raw = (config or {}).get("fw_asset_classes") or {}
    if not isinstance(raw, dict):
        raise ValueError("fw_asset_classes 必须是对象")
    out, owner = {}, {}
    for name, c in raw.items():
        if not isinstance(c, dict):
            raise ValueError(f"资产类别 {name} 必须是对象")
        unknown = set(c) - set(CLASS_DEFAULTS)
        if unknown:
            raise ValueError(f"资产类别 {name} 含未知字段：{sorted(unknown)}")
        full = {**CLASS_DEFAULTS, **c}
        make_calendar(full["calendar"], **(full["calendar_params"] or {}))      # 日历名与参数无效时报错
        for k in ("entry_after_open_min", "entry_before_close_min", "flat_before_close_min", "flat_before_gap_min",
                  "events_max_age_days", "event_before_min", "event_after_min"):
            if float(full[k]) < 0:
                raise ValueError(f"资产类别 {name}：{k} 不能为负")
        for p in full["pairs"]:
            if p in owner:
                raise ValueError(f"交易对 {p} 同时属于资产类别 {owner[p]} 和 {name}")
            owner[p] = name
        out[name] = full
    return out


def is_tradfi_market(market: dict | None, markers: dict | None = None) -> bool:
    """交易所元数据显示这是股票 / 商品等 TradFi 合约（ccxt market["info"] 里的原始字段）。"""
    info = (market or {}).get("info") or {}
    for field, pats in (markers or TRADFI_MARKERS).items():
        raw = info.get(field)
        if raw is None or raw == "":
            continue
        v = str(raw).upper()
        for pat in pats:
            pat = str(pat).upper()
            if (pat.startswith("!") and not fnmatchcase(v, pat[1:])) or (not pat.startswith("!") and fnmatchcase(v, pat)):
                return True
    return False


# ------------------------------------------------------------------ 个股事件文件
def _ts(s) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


_EV_CACHE: dict = {}


def load_equity_events(path: str) -> dict:
    """个股事件文件：
    {"generated_at": "2026-11-20T00:00:00Z",
     "events": [{"symbol": "TSLA", "time_utc": "2027-01-28T21:00:00Z", "type": "earnings"}, ...]}
    返回 {"error", "generated_at", "events": [(symbol, time, type)]}；按修改时间缓存。"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {"error": "事件文件不存在", "events": []}
    hit = _EV_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        data = json.load(open(path, encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ValueError("缺少 events 列表")
        evs = [(str(e["symbol"]).upper(), _ts(e["time_utc"]), str(e.get("type") or "event")) for e in data["events"]]
        info = {"error": None, "generated_at": _ts(data["generated_at"]), "events": evs, "mtime": mt}
    except (OSError, ValueError, KeyError, TypeError) as e:
        info = {"error": f"事件文件无效：{type(e).__name__}", "events": []}
    _EV_CACHE[path] = (mt, info)
    return info


def events_status(cls: dict, now: datetime) -> tuple[str | None, list]:
    """（问题, 事件列表）。问题：未配置 / 不存在 / 无效 / 过期；没有问题为 None。"""
    f = cls.get("events_file") or ""
    if not f:
        return "未配置个股事件文件", []
    info = load_equity_events(f)
    if info["error"]:
        return info["error"], []
    if now - info["generated_at"] > timedelta(days=float(cls["events_max_age_days"])):
        return "个股事件文件过期", info["events"]
    return None, info["events"]


def event_hit(cls: dict, events: list, symbol, now: datetime, before_only: bool = False):
    """now 落在某个事件的 [t − before, t + after] 内时返回 (类型, 时间)；before_only 时只看还没发生的事件。
    symbol 可以是一个代码或代码集合（symbol_aliases）。"""
    syms = {symbol} if isinstance(symbol, str) else set(symbol)
    b, a = timedelta(minutes=float(cls["event_before_min"])), timedelta(minutes=float(cls["event_after_min"]))
    for sym, t, typ in events:
        if sym not in syms:
            continue
        if t - b <= now <= (t if before_only else t + a):
            return typ, t
    return None


# ------------------------------------------------------------------ 时段规则
def entry_block(cls: dict, cal, now: datetime) -> str | None:
    """按交易时段判断能不能新开仓：休市、开盘缓冲、收盘缓冲（含收盘前要平仓的那段）。返回原因或 None。"""
    st = cal.status(now)
    if not st.is_open:
        return "休市"
    if st.minutes_since_open is not None and st.minutes_since_open < float(cls["entry_after_open_min"]):
        return "开盘缓冲"
    if st.minutes_to_close is not None:
        guard = max(float(cls["entry_before_close_min"]), float(cls["flat_before_close_min"]))
        if cls["flat_before_gap"] and st.gap_after_close_days > 1:
            guard = max(guard, float(cls["flat_before_gap_min"]))
        if st.minutes_to_close <= guard:
            return "收盘缓冲"
    return None


def session_exit(cls: dict, cal, now: datetime) -> str | None:
    """持仓的时段离场：fw_session_close（每日收盘前）/ fw_session_gap（长假前或已经身处长休市）。"""
    st = cal.status(now)
    if st.is_open and st.minutes_to_close is not None:
        if float(cls["flat_before_close_min"]) > 0 and st.minutes_to_close <= float(cls["flat_before_close_min"]):
            return "fw_session_close"
        if cls["flat_before_gap"] and st.gap_after_close_days > 1 and st.minutes_to_close <= float(cls["flat_before_gap_min"]):
            return "fw_session_gap"
    if not st.is_open and st.next_open is not None:
        if float(cls["flat_before_close_min"]) > 0:
            return "fw_session_close"                       # 本该收盘前平掉（例如机器人当时没在运行）
        if cls["flat_before_gap"] and st.next_open - now > timedelta(hours=24):
            return "fw_session_gap"
    return None
