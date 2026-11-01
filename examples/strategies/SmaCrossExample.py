"""示例策略（仅用于验证框架能跑通，不代表有正期望，不要直接用于交易）。

做多：日线收盘价在上升的 50 日 SMA 之上，15 分钟收盘价上穿 20 EMA；止损在最近 4 根 15 分钟最低点下方 0.1%。
做空（v2.13）：日线收盘价在下降的 50 日 SMA 之下，15 分钟收盘价下穿 20 EMA；止损在最近 4 根 15 分钟最高点上方 0.1%。
止盈：2R 平掉一半。离场：多单日线收盘跌破 50 日 SMA，空单日线收盘站上 50 日 SMA。
允许哪个方向由配置的 fw_trade_direction / fw_pair_directions 决定，策略里不用管。
回测：freqtrade backtesting -c user_data/config.json --strategy SmaCrossExample --strategy-path examples/strategies
"""
from __future__ import annotations

import os
import sys

from pandas import DataFrame

from freqtrade.strategy import informative

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "user_data", "strategies"))
from framework_base import FrameworkStrategy  # noqa: E402


class SmaCrossExample(FrameworkStrategy):
    timeframe = "15m"
    startup_candle_count = 100

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma50"] = dataframe["close"].rolling(50).mean()
        dataframe["trend_ok"] = ((dataframe["close"] > dataframe["sma50"])
                                 & (dataframe["sma50"] >= dataframe["sma50"].shift(5))).astype(int)
        dataframe["trend_down"] = ((dataframe["close"] < dataframe["sma50"])
                                   & (dataframe["sma50"] <= dataframe["sma50"].shift(5))).astype(int)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = dataframe["close"].ewm(span=20, adjust=False).mean()
        dataframe["swing_low4"] = dataframe["low"].rolling(4).min()
        dataframe["swing_high4"] = dataframe["high"].rolling(4).max()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        up = (df["close"] > df["ema20"]) & (df["close"].shift(1) <= df["ema20"].shift(1))
        down = (df["close"] < df["ema20"]) & (df["close"].shift(1) >= df["ema20"].shift(1))
        long_ = up & (df["trend_ok_1d"] == 1) & (df["volume"] > 0)
        short = down & (df["trend_down_1d"] == 1) & (df["volume"] > 0)
        # 同一列 fw_stop / fw_entry_cap：多单行写下方止损与限价上限，空单行写上方止损与限价下限
        df["fw_stop"] = df["swing_low4"] * (1 - 0.001)
        df["fw_entry_cap"] = df["close"] * (1 + 0.001)
        df.loc[short, "fw_stop"] = df["swing_high4"] * (1 + 0.001)
        df.loc[short, "fw_entry_cap"] = df["close"] * (1 - 0.001)
        df.loc[long_, ["enter_long", "enter_tag"]] = (1, "sma_cross")
        df.loc[short, ["enter_short", "enter_tag"]] = (1, "sma_cross_down")
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def take_profit_legs(self, trade):
        return [(2.0, 0.5, "tp_2R")]

    def strategy_exit(self, pair, trade, current_time, current_rate, last_row):
        if last_row is None:
            return None
        if trade.is_short:
            return "above_sma50" if last_row.get("close_1d", 0) > last_row.get("sma50_1d", float("inf")) else None
        return "below_sma50" if last_row.get("close_1d", 0) < last_row.get("sma50_1d", 0) else None
