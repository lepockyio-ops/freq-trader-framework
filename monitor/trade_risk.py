"""逐笔初始风险与 R（v2.10）：研究矩阵、组合回测、复盘指标共用同一个口径。

只依赖标准库（复盘指标在服务器上独立运行）。输入是 freqtrade 导出的一笔交易（回测导出或 REST API /trades 的字典）。

初始风险 = 入场成交数量 × (开仓价 − 初始止损价)。取值顺序：
  0. 成交时记录的事实（v2.11）：回测时框架写进旁路文件的初始风险（recorded），实盘复盘时从 freqtrade 数据库读出的 fw_init_risk；
     研究端要求每笔都有记录（require_record），缺记录即未知，不再事后推算；
  1. 框架在绑定止损时写下的初始风险（fw_init_risk，字典里有时直接用）；
  2. stop_loss_abs，前提是它像框架止损：在开仓价下方、距离不超过 stop_max_pct，且不低于按计划能得到的最低止损；
  3. 否则用 enter_tag 里的计划止损（fw|s=…）按下单价过护栏得到的止损（floor 模式抬到下限；成交价更优时放宽到下限 + 0.02 个百分点）；
  4. 都不行：未知（R = None），不拿兜底止损充当策略风险。
v2.9 及以前一律用 stop_loss_abs：价格在止损装上之前就穿过（fw_stop_crossed）时，它还是兜底的 −99% ÷ 杠杆，
−2R 的亏损会被算成约 −0.2R（外部审查 v2.9 P1-1）。多单与空单都适用（v2.13，is_short）。数量用入场订单的成交量 filled，而不是下单数量 amount。
"""
from __future__ import annotations

STOP_MIN_PCT = 0.8           # 与 framework_base.FW_DEFAULTS 一致（示例值）
STOP_MAX_PCT = 3.0
WIDEN_EXTRA_PCT = 0.02
TOL = 0.0005                 # 价格精度取整的容差（相对）


def plan_stop(enter_tag) -> float | None:
    """enter_tag 里框架写的计划止损（fw|s=<止损>|…），没有返回 None。"""
    s = str(enter_tag or "")
    if not s.startswith("fw|"):
        return None
    for part in s[3:].split("|"):
        k, _, v = part.partition("=")
        if k == "s":
            try:
                x = float(v)
            except ValueError:
                return None
            return x if x == x and x > 0 else None
    return None


def entry_filled_amount(t: dict) -> float:
    """入场订单的成交量之和（回测导出与 API 都有 filled；缺 filled 时退回订单数量，再退回 trade.amount）。"""
    total = 0.0
    for o in t.get("orders") or []:
        if o.get("ft_is_entry"):
            v = o.get("filled")
            total += float(v if v is not None else (o.get("amount") or 0))
    return total or float(t.get("amount") or 0)


def _short(t: dict) -> bool:
    return bool(t.get("is_short"))


def _dist(entry: float, stop: float, short: bool) -> float:
    """止损距离（比例）；止损在正确一侧时为正：多单在下方，空单在上方（v2.13）。"""
    return ((stop - entry) if short else (entry - stop)) / entry


def _at(entry: float, pct: float, short: bool) -> float:
    """距离入场价 pct% 的止损价（多单在下方，空单在上方）。"""
    return entry * (1 + pct / 100) if short else entry * (1 - pct / 100)


def effective_plan_stop(t: dict, stop_min_pct: float = STOP_MIN_PCT, stop_max_pct: float = STOP_MAX_PCT) -> float | None:
    """计划止损按下单价过护栏（与框架 _order_stop 相同；能成交说明下单时止损合规，距离不足下限只能是 floor 模式）。"""
    sp = plan_stop(t.get("enter_tag"))
    px = float(t.get("open_rate_requested") or t.get("open_rate") or 0)
    short = _short(t)
    if sp is None or px <= 0:
        return None
    dist = _dist(px, sp, short) * 100
    if dist <= 0 or dist > stop_max_pct + 1e-9:
        return None
    if dist < stop_min_pct:
        sp = _at(px, stop_min_pct, short)
    return sp


def initial_stop(t: dict, stop_min_pct: float = STOP_MIN_PCT, stop_max_pct: float = STOP_MAX_PCT) -> tuple[float | None, str]:
    """（初始止损价, 来源）。来源：stop_loss_abs / plan / unknown。多单与空单（v2.13）。"""
    fill = float(t.get("open_rate") or 0)
    if fill <= 0:
        return None, "unknown"
    short = _short(t)
    eff = effective_plan_stop(t, stop_min_pct, stop_max_pct)
    farthest = None
    if eff is not None:                                       # 成交价更优、按成交价算距离不足下限时，框架可能放宽到下限 + 0.02
        widen = _at(fill, stop_min_pct + WIDEN_EXTRA_PCT, short)
        farthest = max(eff, widen) if short else min(eff, widen)
    sl = t.get("stop_loss_abs")
    if sl is not None:
        sl = float(sl)
        dist = _dist(fill, sl, short) * 100
        within = farthest is None or (sl <= farthest * (1 + TOL) if short else sl >= farthest * (1 - TOL))
        if 0 < dist <= stop_max_pct + WIDEN_EXTRA_PCT + 1e-9 and within:
            return sl, "stop_loss_abs"
    if eff is not None and _dist(fill, eff, short) > 0:
        dist = _dist(fill, eff, short) * 100
        return (eff if dist >= stop_min_pct - 1e-6 else _at(fill, stop_min_pct + WIDEN_EXTRA_PCT, short)), "plan"
    return None, "unknown"


def initial_risk(t: dict, stop_min_pct: float = STOP_MIN_PCT, stop_max_pct: float = STOP_MAX_PCT,
                 recorded: dict | None = None, require_record: bool = False) -> tuple[float | None, str]:
    """（初始风险金额, 来源）。stop_min_pct / stop_max_pct 要传策略实际生效的值（v2.11；v2.10 固定用默认值）。"""
    if recorded is not None:
        v = recorded.get("init_risk")
        return (float(v), "recorded") if v is not None and float(v) > 0 else (None, "unknown")
    if require_record:
        return None, "no_record"
    if t.get("fw_init_risk") is not None and float(t["fw_init_risk"]) > 0:
        return float(t["fw_init_risk"]), "fw_init_risk"
    stop, src = initial_stop(t, stop_min_pct, stop_max_pct)
    if stop is None:
        return None, "unknown"
    fill = float(t["open_rate"])
    risk = entry_filled_amount(t) * abs(fill - stop)
    return (risk, src) if risk > 0 and _dist(fill, stop, _short(t)) > 0 else (None, "unknown")


def record_key(pair, open_date) -> tuple[str, int]:
    """旁路记录与导出交易的对应键：（交易对, 开仓时间的 Unix 秒）。"""
    import datetime as _dt

    if isinstance(open_date, (int, float)):
        ts = int(open_date)
    elif hasattr(open_date, "timestamp"):
        ts = int(open_date.timestamp())
    else:
        d = _dt.datetime.fromisoformat(str(open_date).replace("Z", "+00:00"))
        ts = int((d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).timestamp())
    return str(pair), ts


def load_records(path) -> dict:
    """读旁路文件（JSON Lines）→ {record_key: 记录}。文件不存在返回空字典。"""
    import json as _json
    import os as _os

    out = {}
    if path and _os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = _json.loads(line)
                    out[(str(r["pair"]), int(r["open_ts"]))] = r
    return out


def r_multiple(t: dict, **kw) -> tuple[float | None, str]:
    """（R, 风险来源）：净盈亏 profit_abs ÷ 初始风险；风险未知时 R = None。"""
    risk, src = initial_risk(t, **kw)
    if risk is None:
        return None, src
    return float(t.get("profit_abs") or 0) / risk, src
