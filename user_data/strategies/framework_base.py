"""freq-trader 框架基类：把“风控与执行”固定下来，策略只写信号。

继承 FrameworkStrategy 的策略只需要：
  1. 设置 timeframe、startup_candle_count；
  2. populate_indicators()：计算你要的指标（可用 @informative 引入其他周期）；
  3. populate_entry_trend()：在入场信号行设置 enter_long 或 enter_short = 1，并写入列 fw_stop（绝对止损价）；
     可选写入 fw_entry_cap（多头限价上限、空头最低卖价，例如“信号价 × 1.001 / 0.999”），不写则按信号价成交；
  4. 可选：take_profit_legs()（分批止盈）、strategy_exit()（自定义离场）、populate_exit_trend()（离场信号）。

框架负责（策略只要不覆盖下面这些回调，就不能绕过）：
  - 入场计划随订单保存：信号行的 fw_stop / fw_entry_cap / 信号时间写进 enter_tag，存进数据库；
    定仓、杠杆、入场检查、成交后的止损都只读这一份计划，重启后也一样（v2.6）；
  - 止损护栏：按下单价检查，距离必须在 stop_min_pct–stop_max_pct；太近时按 stop_min_mode 放弃或改为下限；
    成交价比下单价更优、距离跌破下限时，止损放宽到下限（风险仍不超过下单时的预算）；
    止损位于错误方向、超出上限、缺少计划或实际风险超预算：立即市价离场（fw_invalid_stop），不留无止损仓位；
  - 仓位：单笔风险 = 权益 × risk_per_trade（含往返成本与资金费预留）；名义价值 ≤ 1 倍权益；
  - 杠杆：强平距离 ≥ liq_to_stop_multiple × 止损距离，且 ≤ max_position_leverage 与交易所上限；
  - 入场前检查：事件窗口、事件日历有效性（来源状态、时间格式、新鲜度、覆盖区间）、新闻守卫、
    总在险风险 ≤ max_open_risk、与已持仓相关性 ≤ corr_threshold。检查本身出错一律拒绝入场；
  - 交易所端止损（按绝对价格）、最长持有天数、Protections（连亏暂停、日/周故障保护、冷却）。
已知限制见 docs/REFERENCE.md 的“已知问题”。
所有数值是示例值，不代表适合任何策略；在子类里用 framework_params 覆盖。需要 freqtrade 2026.10。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute

logger = logging.getLogger(__name__)

# ============================================================== 框架参数（示例值）
FW_DEFAULTS = {
    "risk_per_trade": 0.005,
    "max_open_risk": 0.015,
    "corr_threshold": 0.8,
    "max_position_leverage": 30,
    "liq_to_stop_multiple": 3.0,
    "round_trip_cost": 2 * (0.0005 + 0.0003),       # 每边约 0.05% 手续费 + 0.03% 滑点（以你账户实际费率为准）
    "funding_est": 0.0001 * 3 * 10,                 # 每 8 小时 0.01% × 10 天
    "funding_cap_frac_of_stop": 0.2,                # 资金费预留不超过止损距离的 20%
    # Binance 维持保证金率随档位变化；这里取偏保守的估计值，上线前用交易所档位表核对
    "maint_margin_est": {"BTC": 0.004, "ETH": 0.005, "SOL": 0.01, "BNB": 0.01, "HYPE": 0.025, "_default": 0.025},
    "stop_min_pct": 0.8,
    "stop_max_pct": 3.0,
    "stop_min_mode": "skip",                        # skip / floor；研究方案 stop_floor 通过 S03_RESEARCH_OVERRIDES 切换（只在回测里）
    "max_hold_days": 30,
    "event_times_utc": [],
    "event_before_min": 120,
    "event_after_min": 60,
    "event_file": os.environ.get("S03_EVENTS_FILE", ""),
    "event_file_max_age_days": 3.0,
    "event_file_required": os.environ.get("S03_EVENTS_REQUIRED", "1") == "1",   # 回测时设为 0
    # 新闻事件守卫（Odaily + BlockBeats，可选，只收紧）：按币种暂停新开仓。文件由 s03-news 定时器生成（fwresearch.blockbeats）。
    # 未设置或文件不存在 = 未启用；文件超过 news_max_age_min 未更新 = 忽略（不卡住交易）并记日志。回测不应用。
    "news_file": os.environ.get("S03_NEWS_FILE", ""),
    "news_venue": "binance",                        # 交易所级事件（该交易所被盗、暂停提现等）会封禁全部交易对
    "news_max_age_min": 30.0,
    # 研究用压力方案 delay1：入场信号推迟 N 根 K 线（只在回测 / hyperopt 里生效，实盘与 dry-run 忽略）
    "research_entry_delay_bars": 0,
    # 成交价比下单价更优、按成交价算的止损距离低于下限时怎么办（v2.7，由你决定；研究方案 fill_exit / fill_keep 可对比）：
    #   widen：止损放宽到下限 + 0.02 个百分点（风险仍在预算内，但止损不在原结构位）——v2.6 的行为，默认
    #   exit ：立即离场（最贴近“结构太近就不做”，代价是一笔往返成本）
    #   keep ：保留原结构止损（违反距离下限）
    "better_fill_policy": "widen",
    # 入场单部分成交、撤剩余部分没有完成（freqtrade 拒绝撤单或撤单后仍是挂单）超过这么多秒：记错误日志（示例值）。
    # 只要还有部分成交的入场单挂着，框架就拒绝一切新开仓（不论是否超时）。监控进程另有 critical 告警（entry_partial_stuck）。
    "partial_fill_alert_sec": 60,
    # 引擎采用的止损与框架止损相差多少（相对比例）以内算“已采用”：覆盖交易所按价格精度取整（示例值）
    "stop_adopted_tol": 0.0005,
    # 框架止损保护（v2.10）：freqtrade 的 StoplossGuard 只统计 stop_loss / stoploss_on_exchange / trailing_stop_loss / liquidation，
    # 不统计框架的 fw_stop_crossed、fw_invalid_stop:*（都以 custom_exit 离场）。这里用同样的参数把两类合并计数（与 protections 一致）。
    "stop_guard_trades": 6,
    "stop_guard_lookback_min": 7 * 1440,
    "stop_guard_duration_min": 1440,
}

# 研究方案可以覆盖的参数（v2.8）：由研究脚本通过环境变量 S03_RESEARCH_OVERRIDES（JSON）传入，
# 优先级高于策略的 framework_params，且只在回测 / hyperopt 里生效（实盘与 dry-run 一律忽略）。
# v2.7 用 S03_STOP_MIN_MODE 等环境变量改 FW_DEFAULTS，策略的 framework_params 会把它覆盖掉，方案实际没跑（外部审查 v2.7 P2）。
RESEARCH_KEYS = ("stop_min_mode", "research_entry_delay_bars", "better_fill_policy")
# 压力方案（v2.9，外部审查 v2.8 P2-2）：框架要求的最低集合，策略不能去掉；默认集合里其余的方案，去掉要在
# research_stress_waivers 里写明理由（例如纯市价入场的策略去掉 delay1），理由写进清单和判定报告。
MIN_RESEARCH_STRESS = ("cost2x", "stopslip")
DEFAULT_RESEARCH_STRESS = ("cost2x", "delay1", "stopslip")
RESEARCH_RUNMODES = ("backtest", "hyperopt")
# 止损类离场原因：freqtrade 自带的 + 框架的（v2.10）。研究的 stopslip 压力与框架止损保护都按这一份分类。
ENGINE_STOP_EXITS = ("stop_loss", "stoploss_on_exchange", "trailing_stop_loss", "liquidation")
FW_STOP_EXIT_PREFIXES = ("fw_stop_crossed", "fw_invalid_stop")


def is_stop_exit(reason) -> bool:
    r = str(reason or "")
    return r in ENGINE_STOP_EXITS or r.startswith(FW_STOP_EXIT_PREFIXES)


DIRECTIONS = ("both", "long", "short")


def direction_allows(direction: str, is_short: bool) -> bool:
    """方向设置是否允许这个方向。设置值无效时报错（调用方按“拒绝入场”处理）。"""
    if direction not in DIRECTIONS:
        raise ValueError(f"fw_trade_direction 必须是 {DIRECTIONS} 之一，收到 {direction!r}")
    return direction == "both" or (direction == "short") == bool(is_short)


# ============================================================== 纯函数（便于测试）
def stop_distance(entry: float, stop: float, is_short: bool = False) -> float:
    """止损距离（相对入场价的比例，止损在正确一侧时为正）：多单 (入场 − 止损) / 入场，空单 (止损 − 入场) / 入场。"""
    return ((stop - entry) if is_short else (entry - stop)) / entry


def guarded_stop(entry: float, stop: float | None, fw: dict, is_short: bool = False) -> float | None:
    """框架止损护栏：返回合规的止损价，或 None（放弃入场）。"""
    if stop is None or not math.isfinite(float(stop)) or not math.isfinite(entry) or entry <= 0 or stop <= 0:
        return None
    direction = -1 if is_short else 1
    dist = direction * (entry - stop) / entry * 100
    if dist <= 0:
        return None
    if dist < fw["stop_min_pct"]:
        if fw["stop_min_mode"] != "floor":
            return None
        stop, dist = entry * (1 - direction * fw["stop_min_pct"] / 100), fw["stop_min_pct"]
    if dist > fw["stop_max_pct"]:
        return None
    return stop


def stake_for(equity: float, stop_dist: float, leverage: float, fw: dict = FW_DEFAULTS) -> float:
    """保证金 = 名义价值 / 杠杆；名义价值 = 权益 × 单笔风险 / (止损距离 + 往返成本 + 资金费预留)，且 ≤ 1 倍权益。"""
    funding = min(fw["funding_est"], fw["funding_cap_frac_of_stop"] * stop_dist)
    notional = equity * fw["risk_per_trade"] / (stop_dist + fw["round_trip_cost"] + funding)
    notional = min(notional, equity)
    return notional / max(leverage, 1.0)


def isolated_leverage(stop_dist: float, max_lev: float, mm: float, fw: dict = FW_DEFAULTS, is_short: bool = False) -> int:
    """逐仓杠杆 = min(上限, 交易所上限, floor(1 / (k × 止损距离 × (1 − mm) + mm)))，k = liq_to_stop_multiple。"""
    raw = 1.0 / (fw["liq_to_stop_multiple"] * stop_dist * (1 + mm if is_short else 1 - mm) + mm)
    return int(max(1, min(fw["max_position_leverage"], max_lev, math.floor(raw))))


PLAN_PREFIX = "fw|"


def encode_plan(stop, cap, ts, user: str = "", side: str = "long") -> str:
    """入场计划 → enter_tag（freqtrade 把它存进订单与交易，最长 255 字符）。"""
    sv = f"{float(stop):.10g}" if stop is not None and stop == stop else "nan"
    cv = f"{float(cap):.10g}" if cap is not None and cap == cap else ""
    tag = f"{PLAN_PREFIX}s={sv}|c={cv}|t={int(pd.Timestamp(ts).timestamp())}|d={side}"
    return (tag + f"|u={user}")[:255] if user else tag


def decode_plan(tag) -> dict | None:
    """enter_tag → {"stop", "cap", "t", "user"}；不是框架写的或止损无效时返回 None。"""
    if not isinstance(tag, str) or not tag.startswith(PLAN_PREFIX):
        return None
    parts = {}
    head, _, user = tag[len(PLAN_PREFIX):].partition("|u=")
    for p in head.split("|"):
        k, _, v = p.partition("=")
        parts[k] = v
    try:
        stop = float(parts["s"])
        cap = float(parts["c"]) if parts.get("c") else None
        t = int(parts.get("t") or 0)
    except (KeyError, ValueError):
        return None
    side = parts.get("d", "long")
    if not math.isfinite(stop) or stop <= 0 or side not in ("long", "short") or (cap is not None and (not math.isfinite(cap) or cap <= 0)):
        return None
    return {"stop": stop, "cap": cap, "t": t, "user": user, "side": side}


def _utc(s) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def target_reached(trade, current_rate: float, multiple: float) -> bool:
    """Directional R target for strategy hooks; execution and sizing stay in the parent."""
    stop = trade.get_custom_data("stop_px")
    if stop is None or not math.isfinite(float(stop)) or trade.open_rate <= 0:
        return False
    direction = -1 if getattr(trade, "is_short", False) else 1
    risk = direction * (trade.open_rate - float(stop))
    return risk > 0 and direction * (current_rate - trade.open_rate) >= multiple * risk


# ============================================================== 基类
class FrameworkStrategy(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    process_only_new_candles = True
    minimal_roi = {"0": 100}                   # 关掉默认 ROI 离场
    stoploss = -0.99                           # 仅作兜底；实际止损按 fw_stop 的绝对价格
    use_custom_stoploss = True
    trailing_stop = False
    position_adjustment_enable = True          # 分批止盈用部分平仓
    max_entry_position_adjustment = 0          # 不加仓
    use_exit_signal = True                     # 允许策略用 populate_exit_trend 离场

    is_template = False                        # 空白模板设为 True（框架自检会提示“未配置策略”）
    # 研究：上线判定必须通过的压力方案（随策略而定，v2.7）。可选：cost2x（手续费与滑点加重）、delay1（入场推迟一根 K 线）、
    # stopslip（止损离场再扣额外滑点，事后近似）。例如只用市价入场的策略可以去掉 delay1；止损很窄的策略应保留 stopslip。
    # 改动要写进策略规格（docs/STRATEGY_TEMPLATE.md），并在 research plan / portfolio 之前定好。
    research_stress: tuple = DEFAULT_RESEARCH_STRESS   # 不能去掉 MIN_RESEARCH_STRESS；去掉 delay1 要在下面写理由
    research_stress_waivers: dict = {}                 # 例如 {"delay1": "只用市价单入场，信号到下单没有排队等待"}
    research_stop_slip: float = 0.002          # stopslip 方案每次止损额外扣的滑点（名义价值的比例，示例值）
    framework_params: dict = {}                # 子类覆盖框架参数，例如 {"risk_per_trade": 0.003}

    def pair_enabled(self, pair: str) -> bool:
        """Exact configured USDT futures whitelist; an explicit empty list disables all pairs."""
        config = getattr(self, "config", {}) or {}
        exchange = config.get("exchange", {})
        defaults = [f"{coin}/USDT:USDT" for coin in ("BTC", "ETH", "SOL", "BNB", "HYPE")]
        pairs = exchange.get("pair_whitelist", defaults)
        if not isinstance(pairs, list) or not all(isinstance(p, str) for p in pairs):
            raise ValueError("exchange.pair_whitelist must be a list of exact pair names")
        return (isinstance(pair, str) and re.fullmatch(r"[A-Z0-9]+/USDT:USDT", pair) is not None
                and pair in pairs and not any(re.fullmatch(p, pair) for p in exchange.get("pair_blacklist", [])))

    def trade_direction(self, pair: str | None = None) -> str:
        """这个交易对允许的方向（v2.13）：配置 fw_pair_directions[pair] 优先，否则 fw_trade_direction，默认 both。
        在配置文件里设置（用户设定），不写在策略里：同一个策略可以按账户只做多、只做空或多空都做。"""
        cfg = getattr(self, "config", None) or {}
        per = cfg.get("fw_pair_directions") or {}
        d = per.get(pair) if pair and pair in per else cfg.get("fw_trade_direction", "both")
        direction_allows(d, False)                     # 校验取值（无效时报错）
        return d

    def side_allowed(self, pair: str, is_short: bool) -> bool:
        return direction_allows(self.trade_direction(pair), is_short)

    def bot_start(self, **kwargs) -> None:
        """启动时校验方向设置：取值无效直接报错（不让机器人带着无效设置运行）。子类覆盖时调用 super().bot_start()。"""
        cfg = getattr(self, "config", None) or {}
        self.trade_direction()
        for p in (cfg.get("fw_pair_directions") or {}):
            self.trade_direction(p)
        logger.info("交易方向：默认 %s；按交易对覆盖：%s", self.trade_direction(), cfg.get("fw_pair_directions") or "无")

    def side_enabled(self, side: str, pair: str | None = None) -> bool:
        return side in ("long", "short") and self.side_allowed(pair, side == "short")

    def filter_entry_signals(self, df: DataFrame, metadata: dict) -> DataFrame:
        """Apply the same entry policy to direct research signals and engine analysis."""
        for side in ("long", "short"):
            col = "enter_" + side
            if col not in df:
                df[col] = 0
            if not self.pair_enabled(metadata.get("pair")) or not self.side_enabled(side, metadata.get("pair")):
                df[col] = 0
        conflict = df["enter_long"].eq(1) & df["enter_short"].eq(1)
        df.loc[conflict, ["enter_long", "enter_short"]] = 0
        return df

    @property
    def fw(self) -> dict:
        return {**FW_DEFAULTS, **(self.framework_params or {}), **self._research_overrides()}

    def _runmode(self) -> str:
        return getattr(getattr(getattr(self, "dp", None), "runmode", None), "value", "") or ""

    def _research_overrides(self) -> dict:
        """研究方案的参数覆盖（只在回测 / hyperopt 里生效）。JSON 无效或含未知参数时报错：研究不能悄悄跑错方案。"""
        raw = os.environ.get("S03_RESEARCH_OVERRIDES", "")
        if not raw or self._runmode() not in RESEARCH_RUNMODES:
            return {}
        ov = json.loads(raw)
        if not isinstance(ov, dict):
            raise ValueError("S03_RESEARCH_OVERRIDES 必须是 JSON 对象")
        unknown = set(ov) - set(RESEARCH_KEYS)
        if unknown:
            raise ValueError(f"S03_RESEARCH_OVERRIDES 含不允许的参数：{sorted(unknown)}")
        return ov

    @property
    def protections(self):
        fw = self.fw
        return [
            {"method": "StoplossGuard", "lookback_period": fw["stop_guard_lookback_min"], "trade_limit": fw["stop_guard_trades"],
             "stop_duration": fw["stop_guard_duration_min"], "only_per_pair": False},
            {"method": "MaxDrawdown", "calculation_mode": "equity", "lookback_period": 1440,
             "trade_limit": 1, "stop_duration": 1440, "max_allowed_drawdown": 0.02},
            {"method": "MaxDrawdown", "calculation_mode": "equity", "lookback_period": 7 * 1440,
             "trade_limit": 1, "stop_duration": 7 * 1440, "max_allowed_drawdown": 0.04},
            {"method": "CooldownPeriod", "stop_duration": 60},
        ]

    # ---------------------------------------------------------- 策略可覆盖的钩子
    def take_profit_legs(self, trade: Trade) -> list[tuple[float, float, str]]:
        """分批止盈：[(R 倍数, 占初始仓位比例, 标签)]，例如 [(2.0, 0.5, "tp_2R")]。默认不分批。"""
        return []

    def strategy_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                      last_row: pd.Series | None) -> str | None:
        """自定义离场（返回离场原因字符串）。last_row 为最新一行已分析数据。默认无。"""
        return None

    # ---------------------------------------------------------- 入场计划：随订单保存在 enter_tag
    def advise_entry(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """freqtrade 生成入场信号后，把每个信号行的计划止损、限价上限和信号时间写进 enter_tag。
        之后的定仓、杠杆、入场检查、成交后的止损都只读这份计划，不再“重新找最近一次信号”
        （v2.5 之前的做法：挂单期间出现新信号、成交价更优或重启后，止损可能换成别的或丢失）。"""
        df = super().advise_entry(dataframe, metadata)
        if df is None or df.empty:
            return df
        df = self.filter_entry_signals(df, metadata)
        delay = int(self.fw.get("research_entry_delay_bars") or 0)
        if delay > 0 and self._runmode() in RESEARCH_RUNMODES:
            cols = [c for c in ("enter_long", "enter_short", "fw_stop", "fw_entry_cap", "enter_tag") if c in df.columns]
            df[cols] = df[cols].shift(delay)
            df["enter_long"] = df["enter_long"].fillna(0)
            df["enter_short"] = df["enter_short"].fillna(0)
        m = df["enter_long"].eq(1) | df["enter_short"].eq(1)
        if not m.any():
            return df
        nan = pd.Series(float("nan"), index=df.index)
        stop = (df["fw_stop"] if "fw_stop" in df else nan)[m]
        cap = (df["fw_entry_cap"] if "fw_entry_cap" in df else nan)[m]
        user = df["enter_tag"][m].fillna("").astype(str) if "enter_tag" in df else pd.Series("", index=df.index[m])
        sides = df.loc[m, "enter_short"].map(lambda x: "short" if x == 1 else "long")
        df.loc[m, "enter_tag"] = [encode_plan(sv, cv, tv, "" if uv.startswith(PLAN_PREFIX) else uv, side)
                                  for sv, cv, tv, uv, side in zip(stop, cap, df["date"][m], user, sides)]
        return df

    def _plan_stop(self, entry_tag, rate: float, side: str = "long") -> float | None:
        """按下单价检查计划止损：返回合规的止损价，或 None（不入场）。"""
        plan = decode_plan(entry_tag)
        if plan is None or not rate or rate <= 0 or plan["side"] != side:
            return None
        return guarded_stop(float(rate), plan["stop"], self.fw, side == "short")

    def _mm(self, pair: str) -> float:
        m = self.fw["maint_margin_est"]
        return m.get(pair.split("/")[0], m["_default"])

    # ---------------------------------------------------------- 事件日历（有效性校验，出问题就拒绝入场）
    _events_cache: tuple = (None, None)

    def _load_calendar_file(self, f: str) -> dict:
        """解析并缓存事件文件（按修改时间）。返回 {"error", "times", "ok", "sources", "generated_at", "horizon_days"}。"""
        mt = os.path.getmtime(f)
        if FrameworkStrategy._events_cache[0] == (f, mt):
            return FrameworkStrategy._events_cache[1]
        info: dict = {"error": None, "times": []}
        try:
            data = json.load(open(f, encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ValueError("缺少 events 列表")
            info["times"] = [_utc(e["time_utc"]) for e in data["events"]]
            info["ok"] = data.get("ok")
            info["sources"] = data.get("sources") or {}
            info["generated_at"] = _utc(data["generated_at"]) if data.get("generated_at") else None
            info["horizon_days"] = data.get("horizon_days")
        except (OSError, ValueError, KeyError, TypeError) as e:          # pandas 的日期解析错误是 ValueError 子类
            info = {"error": f"事件文件无效：{type(e).__name__}", "times": []}
        FrameworkStrategy._events_cache = ((f, mt), info)
        return info

    def _calendar(self, now: datetime | None = None) -> tuple[bool, str, list]:
        """返回 (可以入场, 原因, 事件时间列表)。event_file_required=True 时，以下任一情况都拒绝入场：
        未配置或找不到事件文件、文件损坏或时间格式非法、超过 event_file_max_age_days 未更新、
        所有来源失败或某个失败来源没有沿用到事件（count = 0）、事件覆盖区间已经不包含现在。
        有来源失败但沿用了上一版事件时放行（日志告警）。回测（required=False）只用文件里的时间，不因文件问题拒绝。"""
        now_ts = _utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
        times = [_utc(t) for t in self.fw["event_times_utc"]]
        f, req = self.fw["event_file"], self.fw["event_file_required"]
        if not f or not os.path.exists(f):
            return (not req), ("未配置事件文件" if not f else "事件文件不存在"), times
        info = self._load_calendar_file(f)
        times = times + info["times"]
        reason = info["error"] or ""
        if not reason and time.time() - os.path.getmtime(f) > self.fw["event_file_max_age_days"] * 86400:
            reason = "事件文件过期"
        if not reason and info.get("ok") is not True:
            srcs = info.get("sources") or {}
            failed = [k for k, v in srcs.items() if not v.get("ok")]
            if not srcs or len(failed) == len(srcs) or any(int(srcs[k].get("count") or 0) == 0 for k in failed):
                reason = "事件来源失败：" + (",".join(failed) or "ok≠true")
            elif FrameworkStrategy._events_warned != (f, os.path.getmtime(f)):
                logger.warning("事件来源 %s 本轮失败，沿用上一版事件：%s", ",".join(failed), f)
                FrameworkStrategy._events_warned = (f, os.path.getmtime(f))
        hz, gen = info.get("horizon_days"), info.get("generated_at")
        if not reason and hz and gen is not None and now_ts > gen + timedelta(days=float(hz) - 1):
            reason = "事件覆盖区间不含现在"
        if reason and req:
            return False, reason, times
        return True, reason, times

    _events_warned: tuple = (None, None)

    def _in_event_window(self, now: datetime, times: list | None = None) -> bool:
        now_ts = _utc(now)
        for t in (self._calendar(now)[2] if times is None else times):
            if t - timedelta(minutes=self.fw["event_before_min"]) <= now_ts <= t + timedelta(minutes=self.fw["event_after_min"]):
                return True
        return False

    def _event_calendar_stale(self, now: datetime | None = None) -> bool:
        """兼容旧名：True = 日历不能用（会拒绝入场）。"""
        return not self._calendar(now)[0]

    # ---------------------------------------------------------- 入场价、杠杆、仓位（只读 enter_tag 里的计划）
    def custom_entry_price(self, pair, trade, current_time, proposed_rate, entry_tag, side, **kwargs) -> float:
        plan = decode_plan(entry_tag)
        cap = plan["cap"] if plan else None
        return (max(proposed_rate, cap) if side == "short" else min(proposed_rate, cap)) if cap else proposed_rate

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side,
                 **kwargs) -> float:
        try:
            stop = self._plan_stop(entry_tag, current_rate, side)
            if stop is None:
                return 1.0
            return float(isolated_leverage(abs(current_rate - stop) / current_rate, max_leverage, self._mm(pair), self.fw, side == "short"))
        except Exception:
            logger.exception("杠杆计算出错，按 1 倍处理：%s", pair)
            return 1.0

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs) -> float:
        """按实际方向和下单价定仓。fw_entry_cap 限制多头最高买价 / 空头最低卖价；成交后另验风险。
        出错返回 0（不入场）：freqtrade 对这个回调出错时的默认值是 proposed_stake，不能依赖它。"""
        try:
            if not self.pair_enabled(pair) or not self.side_enabled(side, pair):
                return 0
            stop = self._plan_stop(entry_tag, current_rate, side)
            if stop is None:
                return 0                                                   # 没有计划或止损不合规：不入场
            stake = stake_for(self.wallets.get_total_stake_amount(), abs(current_rate - stop) / current_rate, leverage, self.fw)
            if min_stake and stake < min_stake:
                return 0
            return min(stake, max_stake)
        except Exception:
            logger.exception("定仓出错，不入场：%s", pair)
            return 0

    _news_cache: tuple = (None, None)
    _news_warned: str = ""

    def _news_block(self, pair: str, now: datetime) -> str | None:
        """返回封禁原因（如 "security:SOL"），没有封禁返回 None。与 fwresearch.blockbeats.load_blocks 同一口径。"""
        f = self.fw.get("news_file")
        if not f or not os.path.exists(f) or self._runmode() in RESEARCH_RUNMODES:
            return None
        mt = os.path.getmtime(f)
        if FrameworkStrategy._news_cache[0] != mt:
            try:
                FrameworkStrategy._news_cache = (mt, json.load(open(f, encoding="utf-8")))
            except (OSError, ValueError):
                FrameworkStrategy._news_cache = (mt, None)
        data = FrameworkStrategy._news_cache[1]
        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        try:
            gen = datetime.fromisoformat(data["generated_at"])
        except (TypeError, KeyError, ValueError):
            gen = None
        if gen is None or now_utc - gen > timedelta(minutes=self.fw["news_max_age_min"]):
            state = "error" if gen is None else "stale"
            if FrameworkStrategy._news_warned != f"{state}:{mt}":
                logger.warning("新闻事件守卫文件%s，已忽略：%s", "无法读取" if gen is None else "过期", f)
                FrameworkStrategy._news_warned = f"{state}:{mt}"
            return None
        failed = sorted(k for k, v in (data.get("sources") or {}).items() if not v.get("ok"))
        if failed and FrameworkStrategy._news_warned != f"degraded:{mt}":
            logger.warning("新闻来源 %s 本轮采集失败，守卫只用剩下的来源：%s", ",".join(failed), f)
            FrameworkStrategy._news_warned = f"degraded:{mt}"
        coin = pair.split("/")[0].upper()
        for b in data.get("blocks", []):
            if b.get("target") in (coin, f"@{self.fw['news_venue']}") and datetime.fromisoformat(b["until_utc"]) > now_utc:
                return f"{b.get('category')}:{b.get('target')}"
        return None

    def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side,
                            **kwargs) -> bool:
        """下单前最后一道检查。任何异常都按“拒绝入场”处理：freqtrade 对这个回调出错时的默认返回值是 True（放行）。"""
        try:
            return self._confirm_entry(pair, amount, rate, current_time, entry_tag, side)
        except Exception:
            logger.exception("入场检查出错，按拒绝处理：%s", pair)
            return False

    def _confirm_entry(self, pair, amount, rate, current_time, entry_tag, side="long") -> bool:
        if self.is_template or not self.pair_enabled(pair) or not self.side_enabled(side, pair):
            return False
        cal_ok, cal_reason, times = self._calendar(current_time)
        if not cal_ok:
            logger.info("事件日历不可用，拒绝入场：%s（%s）", pair, cal_reason)
            return False
        if self._in_event_window(current_time, times):
            return False
        nb = self._news_block(pair, current_time)
        if nb:
            logger.info("新闻事件守卫：%s 暂停新开仓（%s）", pair, nb)
            return False
        stop = self._plan_stop(entry_tag, rate, side)
        plan = decode_plan(entry_tag)
        if stop is None or not rate or not math.isfinite(rate) or amount <= 0 or not math.isfinite(amount):
            return False
        if plan["cap"] is not None and ((rate < plan["cap"]) if side == "short" else (rate > plan["cap"])):
            return False
        equity = self.wallets.get_total_stake_amount()
        new_risk = amount * abs(rate - stop)
        if new_risk > equity * self.fw["risk_per_trade"] * 1.05:
            return False
        open_trades = Trade.get_trades_proxy(is_open=True)
        stuck = [t.pair for t in open_trades if self._partial_entry_open(t)]
        if stuck:
            logger.info("有部分成交、尚未撤完的入场单（%s）：拒绝新开仓：%s", ",".join(stuck), pair)
            return False
        guard = self._stop_guard_reason(current_time)
        if guard:
            logger.info("框架止损保护：%s，拒绝新开仓：%s", guard, pair)
            return False
        open_risk = self._open_risk(open_trades)
        if open_risk is None:
            logger.info("已有仓位或挂单缺少止损 / 计划，拒绝新增风险：%s", pair)
            return False
        if open_risk + new_risk > equity * self.fw["max_open_risk"] * 1.0001:
            return False
        others = [t for t in open_trades if t.pair != pair]
        if others and not self._corr_ok(pair, others, side == "short"):
            return False
        return True

    @staticmethod
    def _entry_in_flight(t) -> bool:
        """入场单还没走完：没有任何已完成的入场成交，或者还有挂着的入场单（含已部分成交）。"""
        if not getattr(t, "nr_of_successful_entries", 0):
            return True
        side = getattr(t, "entry_side", "buy")
        return any(getattr(o, "ft_order_side", None) == side and getattr(o, "ft_is_open", False)
                   for o in (getattr(t, "orders", None) or []))

    def _stop_guard_reason(self, now: datetime) -> str | None:
        """与 StoplossGuard 同参数，但把框架的止损类离场（fw_stop_crossed、fw_invalid_stop:*）一起计数（v2.10，外部审查 v2.9 P1-2）。
        最近一笔止损类亏损离场之前的回看期内达到 trade_limit 笔、且距今不到 stop_duration：拒绝新开仓。
        只有计数里含框架离场原因时才由这里拒绝（纯 freqtrade 原因由 StoplossGuard 自己处理，结果相同）。"""
        fw = self.fw
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        since = now - timedelta(minutes=fw["stop_guard_lookback_min"] + fw["stop_guard_duration_min"])
        closed = Trade.get_trades_proxy(is_open=False, close_date=since)
        hits = sorted(((t.close_date_utc, str(t.exit_reason)) for t in closed
                       if is_stop_exit(t.exit_reason) and (t.close_profit or 0) < 0), key=lambda x: x[0])
        if not hits:
            return None
        last = hits[-1][0]
        if now >= last + timedelta(minutes=fw["stop_guard_duration_min"]):
            return None
        window = [r for c, r in hits if c >= last - timedelta(minutes=fw["stop_guard_lookback_min"])]
        if len(window) >= fw["stop_guard_trades"] and any(r.startswith(FW_STOP_EXIT_PREFIXES) for r in window):
            return f"{len(window)} 次止损类亏损离场（含框架离场 {sum(r.startswith(FW_STOP_EXIT_PREFIXES) for r in window)} 次）"
        return None

    @staticmethod
    def _partial_entry_open(t) -> bool:
        """有已部分成交、仍挂着的入场单：已成交部分在交易所上没有止损（freqtrade 在入场单挂着时不更新仓位）。"""
        side = getattr(t, "entry_side", "buy")
        return any(getattr(o, "ft_order_side", None) == side and getattr(o, "ft_is_open", False)
                   and float(getattr(o, "filled", 0) or 0) > 0 for o in (getattr(t, "orders", None) or []))

    def _order_stop(self, t) -> float | None:
        """下单时实际生效的止损（v2.8）：计划止损按下单价过护栏（floor 模式会被抬到下限）——与定仓用的是同一个值。
        v2.7 挂单风险用的是计划里的原始止损，floor 模式下比定仓用的止损近，在险风险少算（外部审查 v2.7 P1-1）。"""
        px = getattr(t, "open_rate_requested", None) or getattr(t, "open_rate", None)
        side = "short" if getattr(t, "is_short", False) else "long"
        return self._plan_stop(getattr(t, "enter_tag", None), float(px), side) if px else None

    def _open_risk(self, open_trades) -> float | None:
        """已占用的止损风险（v2.7 修正）：
        - 入场已走完：绑定的止损 × 当前持仓数量 trade.amount；
        - 入场还在进行（未成交或部分成交、单子仍挂着）：止损（已绑定的，否则用下单时生效的止损 _order_stop）× 下单数量 amount_requested，
          价格用下单价 open_rate_requested。freqtrade 新建交易时 amount = 0，挂单期间不会更新它；
          v2.6 乘的是 trade.amount，挂单风险实际按 0 计算（外部审查 v2.6 P1-1）。
        任何一笔既没有绑定止损、也读不出计划（或已判为无效、正在离场）时返回 None：信息缺失时不再新增风险。"""
        total = 0.0
        for t in open_trades:
            in_flight = self._entry_in_flight(t)
            sp = t.get_custom_data("stop_px")
            if sp is None and not in_flight:
                sp = self._bind_stop(t)                     # 已成交但还没绑定（例如刚重启）：现在绑定
                if sp is None:
                    return None
            if sp is None:                                  # 入场还在进行：按下单时生效的止损（与定仓一致）
                if t.get_custom_data("fw_invalid"):
                    return None
                sp = self._order_stop(t)
                if sp is None:
                    return None
            if in_flight:
                qty = max(float(getattr(t, "amount_requested", 0) or 0), float(t.amount or 0))
                px = float(getattr(t, "open_rate_requested", None) or t.open_rate)
            else:
                qty, px = float(t.amount), float(t.open_rate)
            if qty <= 0:
                return None                                 # 读不出数量：不能证明风险为 0
            distance = (-1 if getattr(t, "is_short", False) else 1) * (px - float(sp))
            if not math.isfinite(distance) or distance <= 0:
                return None
            total += distance * qty
        return total

    def check_entry_timeout(self, pair, trade, order, current_time, **kwargs) -> bool:
        """入场单一旦部分成交，就请求撤掉剩余部分（v2.7，外部审查 v2.6 P0）。
        freqtrade 在入场单仍挂着时不更新 trade.amount，has_open_position = False，交易所上不会挂任何止损，
        直到整单成交或超时撤单。撤掉剩余后，已成交部分成为正式仓位，下一轮循环（约 5 秒）绑定止损并挂到交易所。
        返回 True = 按超时撤单（已成交部分保留）。

        注意（v2.9，外部审查 v2.8 P1-1）：订单超过 unfilledtimeout（入场 3 分钟）后，freqtrade 直接按超时处理、
        不再调用本回调，所以撤单失败的监测与告警不能放在这里——在 bot_loop_start 里做（每轮都调用）。"""
        try:
            filled = float(getattr(order, "filled", 0) or 0)
            if filled > 0 and getattr(order, "status", "open") == "open":
                logger.warning("%s 入场单部分成交 %s：撤掉剩余部分，已成交部分立即挂止损", pair, filled)
                return True
        except Exception:
            logger.exception("检查入场单状态出错：%s", pair)
        return False

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """每轮循环开始时调用（不受订单超时分支影响）。子类覆盖时必须调用 super().bot_loop_start(current_time, **kwargs)。"""
        if self._runmode() in RESEARCH_RUNMODES:
            return
        try:
            self._watch_partial_entries(current_time, Trade.get_trades_proxy(is_open=True))
        except Exception:
            logger.exception("部分成交监测出错")

    def _watch_partial_entries(self, current_time: datetime, open_trades) -> list:
        """部分成交、撤单没完成的入场单（v2.9）：首次发现时间存进交易的 custom data（数据库，重启后不丢）；
        超过 partial_fill_alert_sec 仍未解决，记一次 ERROR 并标记 entry_stuck_since。
        这期间已成交部分在交易所上没有止损，freqtrade 也不能对它下离场单；新开仓由 _confirm_entry 拒绝。
        监控进程从 /status 读同样的状态，发 critical 告警（monitor 的 entry_partial_stuck 规则）。返回本轮判为卡住的交易。"""
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        stuck = []
        for t in open_trades:
            since_raw = t.get_custom_data("partial_since")
            if not self._partial_entry_open(t):
                if since_raw and not t.get_custom_data("partial_resolved_at"):
                    t.set_custom_data("partial_resolved_at", now.isoformat())
                    logger.info("%s 部分成交的入场单已结束（成交或撤单完成）", t.pair)
                continue
            if not since_raw or t.get_custom_data("partial_resolved_at"):
                since = now
                t.set_custom_data("partial_since", now.isoformat())
                t.set_custom_data("partial_resolved_at", "")
            else:
                since = datetime.fromisoformat(str(since_raw))
            age = (now - since).total_seconds()
            if age >= self.fw["partial_fill_alert_sec"]:
                stuck.append(t)
                if not t.get_custom_data("entry_stuck_since"):
                    t.set_custom_data("entry_stuck_since", since.isoformat())
                    logger.error("%s 入场单部分成交 %d 秒后剩余部分仍未撤掉：已成交部分在交易所上没有止损。"
                                 "新开仓已暂停；请在交易所核对并手动处理（撤单或补挂止损）", t.pair, int(age))
        return stuck

    def informative_pairs(self):
        """相关性检查需要白名单全部交易对的日线：在这里固定订阅（实盘 dp 只返回订阅过的数据）。
        子类覆盖 informative_pairs 时要把 super().informative_pairs() 加进去。"""
        try:
            pairs = self.dp.current_whitelist()
        except Exception:
            return []
        return [(p, "1d") for p in pairs]

    def _corr_ok(self, pair: str, others, is_short: bool = False) -> bool:
        """与已持仓或挂单是不是同一笔风险（30 日日收益相关系数）：同方向时相关系数 > corr_threshold 拒绝；
        反方向时相关系数 < −corr_threshold 拒绝（例如做空 A、做多与 A 强负相关的 B，等于同向下注；v2.13）。
        反方向且强正相关（对冲）不拒绝，但总在险风险仍按两笔相加计算（偏保守）。
        日线缺失、不足 31 根或算不出相关系数时也拒绝（v2.5 及以前会直接跳过检查）。"""
        closes = {}
        for p in [pair] + [t.pair for t in others]:
            df = self.dp.get_pair_dataframe(p, "1d")
            if df is None or len(df) <= 31:
                logger.info("相关性检查缺少 %s 的日线，拒绝入场：%s", p, pair)
                return False
            closes[p] = df.set_index("date")["close"].iloc[-31:]
        rets = pd.DataFrame(closes).pct_change()
        for t in others:
            c = rets[pair].corr(rets[t.pair])
            same = bool(getattr(t, "is_short", False)) == bool(is_short)
            if c != c or (c if same else -c) > self.fw["corr_threshold"]:
                return False
        return True

    # ---------------------------------------------------------- 成交后：按计划绑定止损（成交、部分成交、重启后都走这里）
    def _bind_stop(self, trade) -> float | None:
        """第一次看到有成交的仓位时，把 enter_tag 里的计划止损绑定到交易上并校验：
        - 多头止损必须低于成交价、空头止损必须高于成交价，距离不超过 stop_max_pct；成交价更优导致距离不足下限时放宽到下限；
        - 实际止损风险不超过 权益 × risk_per_trade（5% 容差，覆盖手续费与取整）；
        不满足（或缺少计划）时记 fw_invalid，custom_exit 立即市价离场。返回止损价或 None。"""
        sp = trade.get_custom_data("stop_px")
        if sp is not None:
            direction = -1 if getattr(trade, "is_short", False) else 1
            if math.isfinite(float(sp)) and float(sp) > 0 and direction * (trade.open_rate - float(sp)) > 0:
                return float(sp)
            trade.set_custom_data("fw_invalid", "cached_stop_wrong_side_or_nonfinite")
            return None
        if trade.get_custom_data("fw_invalid") or not getattr(trade, "nr_of_successful_entries", 1) or not trade.amount:
            return None                                     # 还没有已完成的入场成交：交易所上也还没有仓位
        fw, fill = self.fw, float(trade.open_rate)
        direction = -1 if getattr(trade, "is_short", False) else 1
        plan, reason, stop = decode_plan(trade.enter_tag), "", None
        if plan is None or plan["side"] != ("short" if direction == -1 else "long"):
            reason = "no_plan"
        else:
            stop = self._order_stop(trade) or plan["stop"]    # 下单时生效的止损（floor 模式已抬到下限），与定仓一致
            dist = direction * (fill - stop) / fill * 100 if fill > 0 else -1
            if not math.isfinite(fill) or not math.isfinite(stop) or dist <= 0:
                reason = "stop_not_above_fill" if direction == -1 else "stop_not_below_fill"
            elif dist > fw["stop_max_pct"] + 1e-9:
                reason = "stop_too_far"
            else:
                if dist < fw["stop_min_pct"] - 1e-6:          # 成交价更优，按成交价算的距离跌破下限
                    policy = fw.get("better_fill_policy", "widen")
                    if policy == "exit":
                        reason = "stop_too_close_after_fill"
                    elif policy == "keep":
                        trade.set_custom_data("stop_kept_below_min", True)
                    else:
                        stop = fill * (1 - direction * (fw["stop_min_pct"] + 0.02) / 100)
                        trade.set_custom_data("stop_widened", True)
                elif dist < fw["stop_min_pct"] + 0.02:        # 正好在下限附近（例如 floor 模式）：只为取整留余量，与成交价策略无关
                    stop = fill * (1 - direction * (fw["stop_min_pct"] + 0.02) / 100)
                equity = self.wallets.get_total_stake_amount() if getattr(self, "wallets", None) else None
                if not reason and equity and trade.amount * direction * (fill - stop) > equity * fw["risk_per_trade"] * 1.05:
                    reason = "risk_over_budget"
        if reason:
            trade.set_custom_data("fw_invalid", reason)
            logger.error("交易 %s 的止损无法绑定（%s）：将立即离场", trade.pair, reason)
            self._log_risk(trade, None, reason)
            return None
        trade.set_custom_data("stop_px", float(stop))
        trade.set_custom_data("plan_stop", float(plan["stop"]))
        trade.set_custom_data("order_stop", float(self._order_stop(trade) or plan["stop"]))
        trade.set_custom_data("init_stake", float(trade.stake_amount))
        trade.set_custom_data("init_amount", float(trade.amount))
        trade.set_custom_data("fw_init_risk", float(trade.amount) * direction * (fill - float(stop)))
        self._log_risk(trade, float(stop), "")
        return float(stop)

    def _log_risk(self, trade, stop: float | None, invalid: str) -> None:
        """回测 / hyperopt 时把每笔的初始风险写进旁路文件（v2.11，外部审查 v2.10 P1-2）。freqtrade 的回测导出和 REST 接口
        都不带 custom data，研究端以前只能事后推算（用默认的止损上下限）。路径由研究脚本按每次运行设置（S03_RISK_LOG）。
        止损无效、立即离场的交易：初始风险按下单时生效的止损 × 成交量（这是下单时承担的风险预算）。"""
        path = os.environ.get("S03_RISK_LOG", "")
        if not path or self._runmode() not in RESEARCH_RUNMODES:
            return
        try:
            fw, amt, fill = self.fw, float(trade.amount or 0), float(trade.open_rate)
            px = float(getattr(trade, "open_rate_requested", None) or fill)
            order_stop = self._order_stop(trade)
            if stop is not None:
                risk = amt * abs(fill - stop)
            elif order_stop is not None:
                risk = amt * abs(px - order_stop)
            else:
                risk = None
            rec = {"pair": trade.pair, "is_short": bool(getattr(trade, "is_short", False)), "open_ts": int(trade.open_date_utc.timestamp()), "amount": amt, "open_rate": fill,
                   "order_price": px, "order_stop": order_stop, "stop": stop, "init_risk": risk, "invalid": invalid or None,
                   "widened": bool(trade.get_custom_data("stop_widened")), "kept_below_min": bool(trade.get_custom_data("stop_kept_below_min")),
                   **{k: fw[k] for k in ("stop_min_pct", "stop_max_pct", "stop_min_mode", "better_fill_policy")}}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:                                 # noqa: BLE001  记录失败只影响研究（缺记录 → R 未知 → 证据不足）
            logger.exception("写初始风险记录失败：%s", trade.pair)

    def order_filled(self, pair, trade, order, current_time, **kwargs) -> None:
        if order.ft_order_side == trade.entry_side:
            self._bind_stop(trade)

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit, after_fill,
                        **kwargs) -> float | None:
        sp = self._bind_stop(trade)
        if sp is None:
            return None                                   # 无效仓位由 custom_exit 立即离场
        if ((current_rate >= sp) if getattr(trade, "is_short", False) else (current_rate <= sp)):
            # 价格已经穿过止损：stoploss_from_absolute 会返回 0，freqtrade 把 0 当成无效值丢掉，引擎止损停在兜底价
            # （10 倍杠杆约 −9.9%）。这里不返回 0，由 custom_exit 立即市价离场（v2.9，外部审查 v2.8 P0）。
            return None
        return stoploss_from_absolute(sp, current_rate, is_short=getattr(trade, "is_short", False), leverage=trade.leverage)

    def _stop_adopted(self, trade, sp: float) -> bool:
        """引擎（和交易所端止损）是否已经采用框架止损：trade.stop_loss 不低于 sp（允许按价格精度取整的误差）。"""
        cur = getattr(trade, "stop_loss", None)
        return cur is not None and math.isfinite(float(cur)) and float(cur) > 0 and ((float(cur) <= sp * (1 + self.fw["stop_adopted_tol"]))
                if getattr(trade, "is_short", False) else (float(cur) >= sp * (1 - self.fw["stop_adopted_tol"])))

    def _stop_crossed(self, trade, sp: float | None, rate: float) -> bool:
        """价格已经到达或穿过框架止损，而引擎还没采用这个止损（首次绑定前价格已跌破、重启恢复、成交确认延迟）。
        引擎已采用时交给引擎 / 交易所端止损处理，避免重复离场。"""
        crossed = rate is not None and sp is not None and ((float(rate) >= sp) if getattr(trade, "is_short", False) else (float(rate) <= sp))
        return crossed and not self._stop_adopted(trade, sp)

    # ---------------------------------------------------------- 分批与离场
    TP_MAX_ATTEMPTS = 3

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate, current_entry_profit, current_exit_profit,
                              **kwargs):
        """分批止盈：是否完成按实际成交量判断（初始数量 − 当前数量），不在下单前标记。
        有未完成的订单时不再下新单；同一档下单 TP_MAX_ATTEMPTS 次仍没成交（拒单、低于最小名义等）就跳过并告警。"""
        sp = self._bind_stop(trade)
        legs = self.take_profit_legs(trade)
        direction = -1 if getattr(trade, "is_short", False) else 1
        if sp is None or direction * (trade.open_rate - sp) <= 0 or not legs or getattr(trade, "has_open_orders", False):
            return None
        init_amount = float(trade.get_custom_data("init_amount") or trade.amount)
        exited = 1.0 - float(trade.amount) / init_amount if init_amount > 0 else 0.0
        r_px = abs(trade.open_rate - sp)
        attempts = dict(trade.get_custom_data("tp_attempts") or {})
        skipped = list(trade.get_custom_data("tp_skipped") or [])
        done, cum = [], 0.0
        for r_mult, frac, tag in sorted(legs, key=lambda x: x[0]):
            cum += frac
            if exited >= cum - max(1e-6, 0.02 * frac):     # 允许 2% 的取整误差（交易所按合约最小单位向下取整）
                done.append(tag)
                continue
            if tag in skipped:
                continue
            if direction * (current_rate - trade.open_rate) < r_mult * r_px:
                break
            if attempts.get(tag, 0) >= self.TP_MAX_ATTEMPTS:
                logger.warning("分批止盈 %s 已下单 %d 次仍未成交，跳过：%s", tag, attempts[tag], trade.pair)
                trade.set_custom_data("tp_skipped", skipped + [tag])
                continue
            target_left = init_amount * (1.0 - min(cum, 1.0))
            amount_out = float(trade.amount) - target_left
            if amount_out <= 0:
                continue
            attempts[tag] = attempts.get(tag, 0) + 1
            trade.set_custom_data("tp_attempts", attempts)
            trade.set_custom_data("tp_done", done)
            return -float(trade.stake_amount) * amount_out / float(trade.amount), tag
        trade.set_custom_data("tp_done", done)
        return None

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        sp = self._bind_stop(trade)
        if sp is None and trade.get_custom_data("fw_invalid"):
            return f"fw_invalid_stop:{trade.get_custom_data('fw_invalid')}"
        if self._stop_crossed(trade, sp, current_rate):
            if not trade.get_custom_data("stop_crossed_at"):
                trade.set_custom_data("stop_crossed_at", current_time.isoformat())
                logger.error("%s 价格 %s 已穿过框架止损 %s，引擎止损仍为 %s：立即市价离场（离场失败时每轮重试）",
                             pair, current_rate, sp, getattr(trade, "stop_loss", None))
            return "fw_stop_crossed"
        if current_time - trade.open_date_utc >= timedelta(days=self.fw["max_hold_days"]):
            return "max_hold"
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last = None if df is None or df.empty else df.iloc[-1]
        return self.strategy_exit(pair, trade, current_time, current_rate, last)
