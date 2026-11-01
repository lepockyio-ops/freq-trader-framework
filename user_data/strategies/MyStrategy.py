"""空白策略模板（默认）。按 TODO 填入你的交易规则；填之前框架能运行，但不会产生任何入场。

填写前请先写好 docs/STRATEGY_TEMPLATE.md 里的策略规格。框架负责的部分（止损护栏、仓位、杠杆、
事件窗口、总风险、相关性、交易所端止损、最长持有、Protections）见 framework_base.py，不需要你再写。
改名时：类名、文件名、user_data/config.json 里的 "strategy" 三处保持一致。
"""
from __future__ import annotations

import os
import sys

from pandas import DataFrame

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from framework_base import FrameworkStrategy  # noqa: E402


class MyStrategy(FrameworkStrategy):
    is_template = True                 # 你的策略里删掉这一行（或设为 False）
    timeframe = "15m"                  # TODO：信号周期
    startup_candle_count = 200         # TODO：指标需要的最少 K 线数
    framework_params = {}              # 可选：覆盖框架参数，例如 {"risk_per_trade": 0.003, "stop_max_pct": 2.5}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # TODO 1：计算指标。需要日线等其他周期时，用 @informative("1d") 装饰一个方法（见 examples/）。
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # TODO 2：在入场信号行设置
        #   做多：dataframe.loc[条件, ["enter_long", "enter_tag", "fw_stop"]] = (1, "我的信号", 止损价)    # 止损在入场价下方
        #   做空：dataframe.loc[条件, ["enter_short", "enter_tag", "fw_stop"]] = (1, "我的信号", 止损价)   # 止损在入场价上方
        #   fw_stop 必须是绝对价格；框架会检查止损距离（默认 0.8%–3%），不合规就不入场。
        #   可选 fw_entry_cap：最差可接受的入场价——多单是限价上限（例如突破价 × 1.001），空单是限价下限（例如跌破价 × 0.999）。
        #   允许做多、做空还是都做，由配置文件的 fw_trade_direction（both / long / short）和 fw_pair_directions 决定，策略里不用写。
        dataframe["fw_stop"] = float("nan")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # TODO 3（可选）：离场信号 dataframe.loc[条件, ["exit_long", "exit_tag"]] = (1, "原因")；空单用 exit_short
        return dataframe

    # TODO 4（可选）：分批止盈
    # def take_profit_legs(self, trade):
    #     return [(2.0, 0.5, "tp_2R")]           # 到 2R 卖出初始仓位的一半

    # TODO 5（可选）：自定义离场
    # def strategy_exit(self, pair, trade, current_time, current_rate, last_row):
    #     return "my_reason" if 条件 else None
