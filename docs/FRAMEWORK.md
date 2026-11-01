# freq-trader 框架说明（Binance USDⓈ-M 永续 · freqtrade · 策略留空）

## 框架与策略的分工

| 由框架完成（`user_data/strategies/framework_base.py` 与运维工具；策略只要不覆盖框架回调就不能绕过） | 由你的策略完成（继承 `FrameworkStrategy`） |
| --- | --- |
| 入场计划随订单保存（`enter_tag`）：定仓、杠杆、入场检查、成交后止损只读这一份计划 | `timeframe`（周期只由策略决定，配置里不要写）、`startup_candle_count` |
| 止损护栏：按下单价检查距离在 `stop_min_pct`–`stop_max_pct`；成交后再按成交价检查，不合格立即离场 | |
| 仓位：单笔风险（含成本与资金费预留）、名义价值 ≤ 1 倍权益 | `populate_indicators()`：指标 |
| 杠杆：强平距离 ≥ 3 倍止损距离，且不超过上限与交易所上限 | `populate_entry_trend()`：`enter_long` 或 `enter_short`、`fw_stop`（绝对止损价）、可选 `fw_entry_cap` |
| 交易方向：按配置 `fw_trade_direction` / `fw_pair_directions` 清掉不允许方向的信号，下单前再查一次 | |
| 入场前检查：事件窗口、事件日历有效性、新闻守卫、总在险风险、相关性；检查本身出错一律拒绝 | 可选 `take_profit_legs()`：分批止盈（R 倍数、比例、标签） |
| 交易所端止损（按绝对价格）、最长持有天数、Protections（连亏暂停、日/周故障保护、冷却） | 可选 `strategy_exit()`、`populate_exit_trend()`：离场 |
| 监控告警、官方事件日历、复盘指标、研究矩阵、`s03ft` 命令、OpenClaw 运维、一键部署 | 可选 `framework_params`：覆盖框架默认值 |

## 接入策略

1. 按 `docs/STRATEGY_TEMPLATE.md` 写好规格。
2. 修改 `user_data/strategies/MyStrategy.py`（删掉 `is_template = True`），或另建文件并把 `user_data/config.json`（部署后为 `/etc/s03/ft-config.json`）的 `"strategy"` 改成你的类名。
3. `pytest -q`；`s03ft research plan --idea <id> / run / aggregate`（研究矩阵）；`s03ft research g0`（偏差检查）。
4. 按 G1–G9 逐级上线；`s03ft doctor` 会检查是否仍在用空白模板。

默认策略是空白模板 `MyStrategy`：freqtrade、监控、告警、复盘、OpenClaw 都能运行，但不会产生任何入场；
`s03ft daily` 的 problems 会提示“仍在使用空白策略模板”。示例 `examples/strategies/SmaCrossExample.py` 只用于验证框架能跑通。

## 研究前端（v2.2：找想法 → 筛因子 → 回测 → 统计检验）

`fwresearch/`（两个模板共用）提供想法登记簿、内置因子库（55 个）与因子检验（5 个币时默认跨币种比较）、逐笔 R 的统计检验（Bootstrap、符号翻转、滚动分段、
Deflated Sharpe、PBO）。找想法可选接入 Vibe-Trading，经 `mcp-guard` 白名单代理，只给研究 agent 用，交易类工具一律拒绝。
入口：`s03ft lab …`；详见 `docs/RESEARCH_LAB.md`。

## 注意

- **止损绑定（v2.6）**：信号行的 `fw_stop`、`fw_entry_cap` 和信号时间由框架写进 `enter_tag`（策略自己的 `enter_tag` 保留在 `u=` 之后），随订单存进数据库。
  成交（含部分成交、重启后第一次检查）时按成交价校验：止损在成交价的正确一侧（多单低于、空单高于）、距离不超过上限、实际风险不超过预算；成交价比下单价更优导致距离不足下限时，按 `better_fill_policy` 处理（见下）。
  起点是**下单时实际生效的止损**（v2.8）：计划止损按下单价过护栏，`stop_min_mode = floor` 时已经抬到下限——与定仓、挂单风险用的是同一个值。
  距离正好落在下限到下限 + 0.02 个百分点之间（例如 floor 模式按下单价成交）时，只为价格取整留出 0.02 个百分点，不算“成交价更优”。
  不满足、或仓位没有框架计划（例如手动开仓）：`custom_exit` 立即以 `fw_invalid_stop:<原因>` 市价离场。
- **分批止盈（v2.6）**：每一档是否完成按实际成交量判断（初始数量 − 当前数量，允许 2% 取整误差），不在下单前标记；有未完成订单时不重复下单；
  同一档下单 3 次仍未成交（拒单、低于最小名义等）就跳过并告警。
- **价格已穿过止损（v2.9）**：成交后第一次设置止损时、重启恢复时或成交确认延迟时，价格可能已经跌破框架止损。freqtrade 的 `stoploss_from_absolute` 这时返回 0，而引擎把 0 当成无效值丢掉，止损停在兜底价（−99% ÷ 杠杆，10 倍约 −9.9%），交易所端止损也挂在那里。现在 `custom_stoploss` 不返回 0，`custom_exit` 发现“现价 ≤ 框架止损且引擎止损还没采用它”（`trade.stop_loss` 低于框架止损超过 `stop_adopted_tol` = 0.05%）就以 `fw_stop_crossed` 立即市价离场，记 ERROR 和 `stop_crossed_at`；离场单失败时 freqtrade 每轮重新询问，自动重试。引擎已经采用止损时交给引擎 / 交易所端止损，避免重复离场。
- **多单与空单（v2.13）**：基类 `can_short = True`。框架的每一步都按方向处理：止损护栏（多单止损在下方、空单在上方，floor 模式向外抬到下限）、
  定仓和杠杆（用止损距离的绝对值；强平距离多空用同一近似，模型推断）、`fw_entry_cap`（多单是限价上限、空单是限价下限）、
  成交后绑定（成交价更优：多单更低、空单更高）、交易所端止损（`stoploss_from_absolute(is_short=…)`）、止损穿越（多单价格 ≤ 止损、空单 ≥ 止损）、
  分批止盈（空单目标价在入场价下方）、在险风险、初始风险记录与 R。相关性检查按方向：同向且相关 > 0.8 拒绝；反向且相关 < −0.8 拒绝（等于同向下注）；
  反向强正相关（对冲）不拒绝，但总在险风险仍相加。资金费预留多空相同（空单可能收到资金费，预留偏保守）。
- **交易方向设置（v2.13，用户设定）**：配置 `fw_trade_direction`（`both` / `long` / `short`，默认 `both`）和 `fw_pair_directions`（按交易对覆盖）。
  `advise_entry` 清掉不允许方向的信号（回测与实盘一致），`confirm_trade_entry` 再查一次；取值无效时 `bot_start` 报错、入场检查拒绝。
  子类覆盖 `bot_start` 时要调用 `super().bot_start()`。
- **框架止损保护（v2.10）**：freqtrade 的 StoplossGuard 只统计 `stop_loss`、`stoploss_on_exchange`、`trailing_stop_loss`、`liquidation`，
  框架以 `custom_exit` 发出的 `fw_stop_crossed`、`fw_invalid_stop:*` 不计数。框架在入场检查里按同样的参数（`stop_guard_trades` 6 次、
  `stop_guard_lookback_min` 7 天、`stop_guard_duration_min` 1 天，示例值，与 protections 共用）把两类合并计数：最近一笔止损类亏损离场
  之前的回看期内达到 6 次、且距今不到 1 天，就拒绝新开仓。成交绑定止损时把初始风险写进 `fw_init_risk`（复盘 R 的分母）；回测 / hyperopt 时再把它连同绑定止损、实际生效的止损参数写进研究脚本指定的 `S03_RISK_LOG`（v2.11，研究端 R 的分母，实盘与 dry-run 不写）。
- **总风险（v2.7 修正，v2.8 再修正）**：入场已走完的仓位按绑定的止损 × 持仓数量；入场还在进行（未成交或部分成交）的按**下单时实际生效的止损** × 下单数量（`amount_requested`）和下单价计入。
  freqtrade 新建交易时 `amount = 0`、挂单期间不更新，v2.6 用的是 `amount`，挂单风险实际按 0 计算；v2.7 用的是计划里的原始止损，floor 模式下比定仓用的止损近，风险少算（v2.8 改为同一个值）。
  任何一笔缺止损、缺计划、计划止损按下单价已不合规、数量读不出或已判为无效时，不再新增风险。
- **成交价更优时的止损（v2.7，参数 `better_fill_policy`，由你决定）**：按成交价算的止损距离低于下限时，`widen`（默认，v2.6 的行为）放宽到下限 + 0.02 个百分点，
  风险仍在预算内但止损不在原结构位；`exit` 立即离场；`keep` 保留原结构止损（违反距离下限）。研究方案 `fill_exit`、`fill_keep` 可与 `base` 做组合回测对比，
  看交易数、亏损尾部、成本和回撤，不只看收益。决定后写进 `framework_params` 和策略规格。
- **部分成交（v2.7，v2.8 补上撤单失败的情形）**：freqtrade 在入场单仍挂着时不更新持仓数量，交易所上不挂任何止损。框架在 `check_entry_timeout` 里发现入场单已部分成交，就撤掉剩余部分；
  已成交部分随即成为正式仓位，下一轮循环（约 5 秒）绑定止损并挂到交易所。
  **撤单不一定完成**：已成交部分低于交易所最小下单额时 freqtrade 拒绝撤单（否则会留下无法平仓的碎仓），交易所也可能没确认撤单。这期间已成交部分没有止损，而且 freqtrade
  不能对它下离场单（还没有仓位）。框架的处理：只要还有部分成交的入场单挂着，**拒绝一切新开仓**；`bot_loop_start` 每轮检查（v2.9：订单超过 `unfilledtimeout` 后 freqtrade 不再调用 `check_entry_timeout`，v2.8 放在那里的告警会被跳过；首次发现时间存进数据库，重启不丢），超过 `partial_fill_alert_sec`（60 秒，示例值）仍未解决，
  记 ERROR 日志（监控按错误告警），并在交易上记 `entry_stuck_since`，需要你在交易所核对并手动处理（撤单或补挂止损）。
  没有止损的部分有多大**不确定**：freqtrade 拒绝撤单时是低于最小下单额的那一小部分；但撤单请求发出、交易所没有确认时，已成交部分可以是任意大小
  （v2.9 文档写“最多是低于最小下单额的部分”，不对）。它不是完整修复，不支持无人值守。人工处理步骤：① 在交易所核对持仓与挂单；
  ② 撤掉剩余挂单，或为已成交部分补挂只减仓的止损单；③ 在 FreqUI 核对交易状态与持仓一致后再恢复新开仓。
  可选的根本办法（模型推断，**未验证**）：入场单用 IOC（`order_time_in_force` 的 entry 设为 IOC），没成交的部分由交易所立即撤掉，
  不会出现“部分成交后挂着”的状态；代价是入场单不能挂着等价格。Binance 期货是否接受、dry-run 怎么模拟、freqtrade 怎么处理 IOC 部分成交，
  要先在测试网验证，所以没有改成默认。
- **研究方案的参数（v2.8）**：`stop_floor`、`delay1`、`fill_exit`、`fill_keep` 通过环境变量 `S03_RESEARCH_OVERRIDES` 传入，优先级高于策略的 `framework_params`，
  只允许 `stop_min_mode`、`research_entry_delay_bars`、`better_fill_policy` 三个参数，只在回测 / hyperopt 里生效。v2.7 用 `S03_STOP_MIN_MODE` 等环境变量改默认值，
  策略在 `framework_params` 里写了同名参数就会把方案覆盖掉（方案等于没跑）；这些旧环境变量已不再读取，实盘与 dry-run 不受任何研究环境变量影响。
- **相关性（v2.6）**：框架在 `informative_pairs()` 里固定订阅白名单全部交易对的日线（子类覆盖时要合并 `super().informative_pairs()`）；
  日线缺失、不足 31 根或算不出相关系数时拒绝入场。回测需要先下载 1d 数据。
- **周期**：`timeframe` 只写在策略里。配置里写了会覆盖策略（freqtrade 规定配置优先）；`s03ft doctor` 会检查。
- **事件日历**：`event_file_required` 为真（实盘默认）时，未配置或找不到文件、文件损坏或时间非法、超过 3 天未更新、所有来源失败、
  某个失败来源没有沿用到事件、覆盖区间不含现在，都拒绝入场。部分来源失败但沿用了上一版事件时放行并告警。
- 框架的止损用 freqtrade 的 `stop_loss_abs`；`initial_stop_loss_abs` 是兜底的 −99% 止损，不代表策略止损（复盘指标已按此计算 R）。
- `s03` 是框架的命名空间（包名、服务名、agent 名），与策略无关。原 S-03 策略在 git 历史 v1.x 中。
