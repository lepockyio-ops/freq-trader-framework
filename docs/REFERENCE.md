# freq-trader 参考手册（文件、已验证项、监控、已知问题）

快速上手见仓库根目录的 `README.md`；给 AI agent 的守则见 `AGENTS.md`；OpenClaw 落地见 `OPENCLAW.md`。

## 文件

| 路径 | 作用 |
| --- | --- |
| `user_data/strategies/framework_base.py` | **框架基类** `FrameworkStrategy`：止损护栏、仓位、杠杆、入场前检查、交易所端止损、分批、最长持有、Protections |
| `user_data/strategies/MyStrategy.py` | **空白策略模板**（默认） |
| `examples/strategies/SmaCrossExample.py` | 示例策略（只用于验证框架） |
| `docs/STRATEGY_TEMPLATE.md`、`docs/FRAMEWORK.md` | 策略规格填写模板、框架说明 |
| `user_data/config.json` | Binance 期货、逐仓、5 个交易对（含 HYPE）、最多 3 笔、交易所端市价止损、dry_run、`"strategy"` |
| `user_data/config-private.example.json` | 密钥与 API 服务器模板（本地调试用） |
| `user_data/config-test-okx.json` | OKX 配置：本地试跑、或所在地区访问不了 Binance 时下载数据 / 回测用 |
| `monitor/` | 行情监控与告警、白名单代理、官方事件日历、复盘指标 |
| `research/matrix.py` | 研究矩阵（策略名读配置或 `FW_STRATEGY`） |
| `s03ft/` | 给 OpenClaw agent 的统一命令行 |
| `deploy/` | 一键部署、systemd、sudoers、4 个 agent 的工作区与技能 |
| `fwresearch/` | 研究前端：想法登记簿、因子库与检验、统计检验、Vibe-Trading 桥接与白名单代理（与 hyper-trader 共用） |
| `tests/` | 约 280 个测试：框架护栏（随机性质测试）、空白模板、示例策略、freqtrade 实际回测、监控、部署、命令行、研究前端 |

## 测试环境里已验证（v2.7–v2.13，2026-10-30，OKX 永续数据代替 Binance）

| 项目 | 结果 |
| --- | --- |
| 周期 | 回测日志不再出现 “Override strategy 'timeframe'”；入场都在 15 分钟 K 线开盘时刻 |
| 示例策略回测（SOL、BTC，2026-10-05 至 10-26） | 9 笔；成交后止损距离 0.82%–1.70%（v2.5 被配置改成 1m 时为 7 笔、0.72%–0.98%） |
| 组合回测 `research portfolio --all`（同一区间） | base、cost2x、delay1 三个方案都成功运行；base 8 笔；上线判定为“不通过”（压力方案期望 R ≤ 0），并记录了“没有历史事件文件”的数据缺口 |
| v2.8 研究流程（同一区间，示例策略 SmaCrossExample） | `portfolio --all` 三个方案的运行记录都带实验标识与开始时间；数据缺口查出 ETH、BNB、HYPE 没有 15m K 线；`delay1` 通过 `S03_RESEARCH_OVERRIDES` 生效（结果与 base 不同）；研究矩阵 `stop_floor` 与 base 结果不同；在回测之后登记的样本外起点被判为“不是事先登记”；只有约 1 个月数据时 PBO 算不出 → 列为“无法判定” |
| v2.9 研究流程（同一区间） | 没带 `--idea` 的组合回测被拒；样本外在第一次回测前登记，判定的登记核对通过；组合回测与矩阵共 8 次运行自动记入登记簿，识别为 2 个参数组合；基准结果与 v2.8 相同（8 笔，−185.19 USDT），说明止损穿越处理没有改变正常交易 |
| v2.10 研究流程（同一区间） | `portfolio --all --trials base,stop_floor` 四个方案都成功；基准结果仍为 8 笔、−185.19 USDT；PBO 的候选来自组合回测，开发区间只含样本外起点之前的 7 笔（第 8 笔被排除）；只有 3 周数据，PBO 算不出 → 列为“无法判定”；8 笔的风险来源都是 `stop_loss_abs` |
| v2.11 研究流程（同一区间） | base、cost2x、stop_floor 共 32 笔，R 的分母全部来自回测时的初始风险记录（`recorded`），未知 0 笔；基准结果仍为 8 笔、−185.19 USDT |
| v2.12 研究流程（同一区间） | `portfolio-verify --variant base` 与 `verify --job SOL-2026-base` 都报告“完整复现”（成交哈希、结果哈希、版本一致，未知 R 0 笔）；登记簿行号：样本外登记第 1 行、第一次回测第 2 行；手填离散度 0.01 小于自动值，未采用 |
| v2.13（freqtrade 2026.10，同一区间） | 示例策略多空都做：base 10 笔（多 8、空 2），多单结果与 v2.12 相同（8 笔、−185.19 USDT）；止损离场的空单约 −1.03R；`fw_trade_direction` 设为 long / short 时只出现对应方向；10 笔的初始风险都有记录，`portfolio-verify` 完整复现 |
| 框架保证的故障测试 | `tests/test_framework_guarantees.py`：用 freqtrade 自己的 `LocalTrade` / `Order` 对象覆盖挂单、部分成交、撤单后绑定、风险预算、日历失效、回调出错等情形 |

示例结果只说明框架能跑通，不说明示例策略有效。

## 行情监控与告警

```bash
cp monitor/monitor.example.toml monitor/monitor.toml
export FT_API_USER=... FT_API_PASS=...          # freqtrade api_server 账号，只给监控进程
python monitor/run_monitor.py alerts            # Binance WebSocket 盯盘，按阈值告警；不消耗 LLM token
python monitor/run_monitor.py gate              # OpenClaw 白名单代理（127.0.0.1:8765）：GET /status、POST /pause
```

- 告警核心 `monitor/alerts_core.py` 与 Hyperliquid 版 `s03/alerts/core.py` 是同一份代码。
- 告警只在触发时交给 OpenClaw（官方 webhook `/hooks/agent`），每小时最多 6 次（示例值），critical 不占额度。
- OpenClaw 唯一能做的动作是经代理调用 freqtrade 的 `/stopentry`（只停新开仓）。它拿不到 freqtrade API 账号和交易所密钥。
- `[openclaw] enabled` 默认为 false；打开前设置 `OPENCLAW_HOOKS_TOKEN` 和 `OPENCLAW_GATE_TOKEN`。
- 未测：测试环境连不上 Binance，WebSocket 地址（`/market/stream`）和 REST 预热需在你本机确认；
  dry-run 下 freqtrade 是否在 `/status` 的 orders 里显示模拟止损单也要确认，否则把 `require_exchange_stop` 设为 false。

## 已知问题

- **部分成交（部分修复，v2.7–v2.8）**：入场单部分成交、仍挂着时，交易所上没有止损。框架会撤掉剩余部分，撤单完成后立即挂止损。
  **未解决的情形**：已成交部分低于最小下单额时 freqtrade 拒绝撤单，或交易所没确认撤单，单子会一直挂着、已成交部分没有止损，freqtrade 也不能对它下离场单。
  v2.8 起这期间拒绝一切新开仓；v2.9 起在 `bot_loop_start` 每轮检查（不受 3 分钟超时分支影响），超过 60 秒（示例值）记 ERROR，监控进程从 `/status` 发 critical 告警 `entry_partial_stuck`，需要人工在交易所处理。见 `docs/FRAMEWORK.md`。
- **freqtrade 2026.10 的变化（v2.13 升级时核对；发布于 2026-10-30）**：**需要 Python ≥ 3.12**（不再支持 3.11；`install.sh` 会检查，旧的 3.11 虚拟环境要删掉重建）。策略的超参数改为按实例保存（要用 `self.<参数>.value` 读取；本框架与示例策略不用超参数）；没有写交易模式时回测默认现货（本模板的配置都写了 `futures`）；Binance / OKX 合约启用盘口 WebSocket。OKX 启动时（回测也一样）会读一次全部永续的行情快照来换算杠杆档位，需要联网；
  freqtrade 的同步请求不读系统代理和证书设置时会失败（本机专属附加配置里给 `ccxt_config` 加 `"requests_trust_env": true`，并设置
  `REQUESTS_CA_BUNDLE`；只在需要代理的机器上，不进仓库）。没有成交的交易 `close_profit` 由 0 改为空值（框架止损保护已按此处理）；
  custom data 的键改为不区分大小写的精确匹配；回测的最小 / 最大下单额按杠杆计算。回测缓存仍不含框架基类，研究脚本与测试一律 `--cache none`。
- **空单（v2.13 新增，未在实盘验证）**：强平距离多空用同一近似；资金费预留多空相同；Binance 空单的交易所端止损类型与触发价沿用多单配置
  （`stoploss_price_type: mark`），上线前要在 dry-run 中确认空单止损单按预期挂出。
- **候选证据、复核与复盘口径（v2.12 已修）**：v2.11 候选有未知 R 时手填 Sharpe 离散度就能让判定通过；`verify` 只比对成交哈希；
  复盘缺数据库记录时按默认参数推算并与有记录的交易混算；登记顺序用时间戳比较（时钟精度可能相同）；指纹路径在 Windows 上用反斜杠。
  现在候选风险完整性是独立一项、手填值不能替代；复核比较最终 R；复盘只用记录、估算单列；顺序用登记簿行号；指纹路径统一为正斜杠。
- **R 未知的交易（v2.11 已修）**：v2.10 的风险模块对算不出初始风险的交易返回“未知”，统计层却直接删掉，亏损可以不留痕迹地消失、判定仍“通过”。
  现在逐层清点，任何一笔未知最多“证据不足”。研究端的分母改用回测时框架记下的初始风险（`risk.jsonl`），复盘端读 freqtrade 数据库；
  事后推算改为使用策略实际的止损上下限。PBO 周网格不再漏掉开头的交易；版本指纹补上风险模块。
- **R 的口径与框架止损类离场（v2.10 已修）**：v2.9 用 `stop_loss_abs` 算 R，`fw_stop_crossed` 离场时它还是兜底价，−2R 算成约 −0.2R；研究端用下单数量、
  复盘端用成交数量。现在三条链路共用 `monitor/trade_risk.py`。`fw_stop_crossed`、`fw_invalid_stop:*` 也计入 `stopslip` 压力和框架止损保护。
- **监控读取失败（v2.10 已修）**：v2.9 读不到 `/status` 时传空列表，部分成交和缺止损的计时都被清零；现在读不到时保留计时，连续 60 秒发 `ft_status_stale` 告警。
- **价格已穿过止损（v2.9 已修）**：成交后首次设置止损时价格已跌破框架止损，v2.8 的 `custom_stoploss` 返回 0 被 freqtrade 丢弃，引擎止损停在兜底价（10 倍杠杆约 −9.9%）。现在立即以 `fw_stop_crossed` 市价离场，监控把这类离场标为 critical。
- **监控 `stop_missing` 误报（v2.9 已修）**：v2.8 把数量为 0 的挂单（带兜底止损价）当成“有持仓缺止损”，普通挂单超过 20 秒也会告警；现在跳过挂单，部分成交卡住的单子单独告警。
- **挂单风险（v2.7–v2.8 已修）**：v2.6 挂单风险按 0 计算（freqtrade 挂单期间 `amount = 0`），v2.7 改为按下单数量；v2.8 起止损用下单时实际生效的止损（floor 模式已抬到下限），与定仓一致。
- **研究流程（v2.7–v2.8 已修）**：上线判定只有 `research verdict` 一个入口；必需压力方案由策略声明；样本外起点须事先登记（早于回测开始）、落在回测区间之内、按入场时间归属；
  只采用同一实验（版本指纹、策略、时间段、交易对、必需方案）的运行，缺哈希、清单过期一律不采用；上线判定要求 PBO；失败或中途出错的组合回测不会留下可用的旧结果；
  数据缺口检查起点、终点与区间内部断档；研究方案的参数优先于策略的 `framework_params`，与 base 相同的方案不运行。见 `docs/RESEARCH_LAB.md`。
- **`enter_tag` 被框架占用**：每笔交易的标记都不同，freqtrade 回测报告、FreqUI 里按入场标记分组会失效。复盘指标（`s03ft metrics`）按 `u=` 之后的策略标记分组。
- **止损绑定（v2.6 已修）**：v2.5 及以前在成交时重新寻找“最近一次信号”，挂单期间出现新信号、成交价更优或重启后，止损可能换成别的或缺失（只剩 −99% 兜底）；按计划入场价检查护栏，成交后距离可能低于下限。现在计划随订单保存，成交后按成交价校验，不合格立即离场，见 `docs/FRAMEWORK.md`。
- **复盘 R 的口径（v2.0 修正）**：R 用 `stop_loss_abs` 计算。v1.x 用的是 `initial_stop_loss_abs`（兜底的 −99% 止损），会把 R 算得过小；v2.0 已改正。
- **维持保证金率**：杠杆公式用的是 `framework_base.FW_DEFAULTS` 里的估计值，上线前用 Binance 的杠杆档位表核对。
- **资金费率与参考价**只在监控进程里告警，入场前不检查；需要的话在你的策略里加条件。
- **前视偏差**：每个新策略都要跑 `s03ft research g0`（lookahead-analysis + recursive-analysis）。

本代码是研究与流程工具，不构成投资建议，不保证收益。
