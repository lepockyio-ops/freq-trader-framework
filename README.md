# freq-trader：freqtrade 自动交易框架（Binance USDⓈ-M 永续 · 模板版 v2.13 · 策略留空）

这是一个基于 [freqtrade](https://www.freqtrade.io/) 2026.10、**不含交易策略的框架**，交易规则由你写成一个继承 `FrameworkStrategy` 的策略接入。框架已经提供：

- 风控：止损护栏、按风险定仓、逐仓杠杆与强平距离、事件窗口、总风险与相关性、交易所端止损、Protections；
- 监控告警、官方事件日历、复盘指标；
- 研究前端：找想法、筛因子、统计检验；
- 由 OpenClaw agent 运营，以及一键部署脚本。

默认交易对是 BTC、ETH、SOL、BNB、HYPE 五个 USDT 永续，`dry_run = true`。默认策略是空白模板 `MyStrategy`：可以运行，但不会开任何仓位。

> **风险提示**：本仓库是研究与流程工具，不构成投资建议，不保证收益。任何策略都必须先通过 G1–G8 验证门槛，实盘（关闭 dry_run）只能由你本人操作（G9）。

**你是 AI agent？** 先读 [`AGENTS.md`](AGENTS.md)。要把整套流程交给 OpenClaw 运营，读 [`OPENCLAW.md`](OPENCLAW.md)。

---

## 目录

1. [这个仓库能做什么、不能做什么](#1-这个仓库能做什么不能做什么)
2. [准备环境](#2-准备环境)
3. [10 分钟本地试跑（不需要任何密钥）](#3-10-分钟本地试跑不需要任何密钥)
4. [配置清单：要改哪些文件](#4-配置清单要改哪些文件)
5. [接入你的策略（3 步）](#5-接入你的策略3-步)
6. [从想法到上线的完整流程](#6-从想法到上线的完整流程)
7. [可选：接入 Vibe-Trading 找想法](#7-可选接入-vibe-trading-找想法)
8. [部署到服务器、交给 OpenClaw 运营](#8-部署到服务器交给-openclaw-运营)
9. [日常命令速查](#9-日常命令速查)
10. [常见问题](#10-常见问题)
11. [文档地图](#11-文档地图)

---

## 1. 这个仓库能做什么、不能做什么

| 能做 | 不能做 / 不会做 |
|---|---|
| 把你的交易规则变成 freqtrade 策略，回测、dry-run、实盘用同一份代码 | 替你想出能赚钱的策略；示例策略 `SmaCrossExample` 只用来验证框架能跑通 |
| 对任何策略执行风控（策略不覆盖框架回调时）：止损随订单绑定、止损距离 0.8–3%、单笔风险、总风险（含挂单）、强平距离 ≥ 3 倍止损、相关性、事件窗口、最长持有；检查出错一律拒绝入场。保证范围与已知缺口见 `docs/FRAMEWORK.md`、`docs/REFERENCE.md` | 让策略绕过这些风控；保证回测与实盘成交一致 |
| 研究前端：登记想法 → 筛因子（跨币种比较）→ 研究矩阵 → 统计检验（Deflated Sharpe、PBO 等） | 把统计检验“通过”当作收益保证 |
| 由 OpenClaw agent 通过统一命令 `s03ft` 运维、研究、复盘 | 让 agent 接触交易所密钥、改配置、恢复交易或关闭 dry_run |

阈值标注贯穿整个仓库：**用户设定** = 你决定；**示例值** = 默认值，未经验证；**经数据验证** = 有回测证据。

## 2. 准备环境

| 需要 | 版本 / 说明 | 必须？ |
|---|---|---|
| Linux（服务器按 Ubuntu / Debian 写）或 macOS | — | 是 |
| Python | **3.12 及以上**（freqtrade 2026.10 不再支持 3.11；Ubuntu 24.04 / Debian 13 自带 3.12+，Ubuntu 22.04 需另装） | 是 |
| freqtrade | 2026.10（`pip install -e ".[trade]"` 会装这个版本） | 是 |
| 网络 | 能访问 Binance 合约 API。所在地区访问不了时，本地试跑用 `user_data/config-test-okx.json`（OKX 公开数据） | 是 |
| Binance API Key | 只勾选“读取”和“合约交易”，**不开提现**，限制服务器 IP；只在 dry-run 之后需要 | 后期 |
| OpenClaw | 2026.10.8 已验证；**需要 Node ≥ 24.22.1 或 ≥ 26.12.1** | 部署时 |
| Vibe-Trading | `vibe-trading-ai==0.1.18`，装在独立虚拟环境 | 可选 |

**密钥规则**：交易所 Key 只放在服务器上 root 0600 的 `/etc/s03/ft-trader.env`，不写进仓库，也不贴进任何聊天（包括发给 AI）。

## 3. 10 分钟本地试跑（不需要任何密钥）

```bash
git clone <你的仓库地址> freq-trader && cd freq-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[trade,test]"                     # 安装 freqtrade 2026.10 和统一命令 s03ft
pytest -q                                          # 约 350 个测试；没有本地行情数据时，少数回测测试会跳过

# 本地试跑用 OKX 公开数据（不需要账号）；有 Binance 访问的话把配置换成 user_data/config.json
export S03_FT_CONFIG=user_data/config-test-okx.json
freqtrade download-data -c $S03_FT_CONFIG --timeframes 1d 15m 1m --timerange 20250101- --trading-mode futures

# 筛因子：5 个币 → 自动用跨币种比较，报告写到 research/lab/
s03ft lab factors --tf 1d

# 自检：空白模板时会有一项 ✗“仍是空白模板”，这是正常的
s03ft doctor --research

# 用示例策略回测，确认整条链路能跑通（结果不代表可交易）
freqtrade backtesting -c $S03_FT_CONFIG --strategy SmaCrossExample --strategy-path examples/strategies \
  --timerange 20261001- -p SOL/USDT:USDT BTC/USDT:USDT
```

这几条命令在 2026-10-28 的一台干净机器上跑通过：5 个币的日线来自 OKX，HYPE 从 2025-02-21 上市起有数据。

## 4. 配置清单：要改哪些文件

| 文件 | 改什么 | 谁来改 | 默认 |
|---|---|---|---|
| `user_data/config.json`（部署后是 `/etc/s03/ft-config.json`） | `"strategy"`（你的策略类名）、`pair_whitelist`（交易对）、`fw_trade_direction` 与 `fw_pair_directions`（交易方向）、`max_open_trades`、`stake_currency` | 你 | `MyStrategy`；BTC、ETH、SOL、BNB、HYPE；多空都做（`both`）；最多 3 笔；`dry_run: true` |
| `user_data/strategies/MyStrategy.py` | 你的策略 | 你 | 空白模板 |
| `user_data/strategies/framework_base.py` 的 `FW_DEFAULTS` | 框架风险参数（单笔风险、总风险、杠杆上限、维持保证金估计）；也可以在策略的 `framework_params()` 里覆盖 | 你（谨慎） | 均为示例值 |
| `/etc/s03/ft-trader.env` | Binance API Key、freqtrade API 账号（`install.sh` 生成模板） | 只能是你 | 空 |
| `/etc/s03/events.env`、`/etc/s03/heartbeat.env` | BLS 要求的联系邮箱、外部心跳地址 | 你 | 空 |
| `monitor/monitor.toml`（由 `monitor.example.toml` 复制） | 告警阈值、交易对 | 你 | 5 个交易对，示例阈值 |
| OpenClaw 配置（合并 `deploy/openclaw/openclaw.s03.json5`） | hooks token、模型 id；装了 Vibe-Trading 时把 `mcp.servers["vibe-trading"].enabled` 改为 `true` | 你 | Vibe-Trading 关闭 |
| `/etc/s03/blockbeats.env` | BlockBeats API Key（只放这一个）；新闻守卫（与 Odaily 同时必需）和研究 MCP 共用 | 你 | 启用新闻守卫时必填 |
| `/etc/s03/vibe-trading.env` | 只放大模型配置，绝不放交易所密钥 | 你 | 不填也能用因子库和行情工具 |

**增减交易对**：
1. 在 `config.json`（服务器上是 `/etc/s03/ft-config.json`）的 `exchange.pair_whitelist` 里删掉或追加交易对，写法是 `XXX/USDT:USDT`（Binance USDⓈ-M 永续）。
2. 同步修改 `monitor/monitor.toml`（服务器上是 `/etc/s03/monitor.toml`）的 `pairs`，两边必须一致（`s03ft doctor` 会检查）。
3. 下载新币的数据：`freqtrade download-data -c <配置> -p XXX/USDT:USDT --timeframes 1d 15m --trading-mode futures`（相关性检查需要日线）。
4. 新币可以在 `framework_base.py` 的 `maint_margin_est` 里补维持保证金率（以 Binance 杠杆档位表为准），否则按 `_default`（2.5%，估计值）。
5. 重启 freqtrade（`sudo systemctl restart s03-freqtrade`）和监控（`s03-ft-monitor`）。新币上线前同样要过研究流程和 dry-run。

**交易方向**（v2.13，用户设定）：在同一个配置文件里设置，不需要改策略。
- `"fw_trade_direction": "both"`：多单和空单都做（默认）；`"long"`：只做多；`"short"`：只做空。
- `"fw_pair_directions": {"HYPE/USDT:USDT": "long"}`：按交易对覆盖（没写的交易对用 `fw_trade_direction`）。
- 不允许的方向：策略的信号在生成时就被清掉，下单前框架再查一次；取值无效时机器人启动报错，入场检查一律拒绝。回测也按这个设置执行。
- 策略里用 `enter_long` / `enter_short` 写信号，止损写在 `fw_stop`：多单在入场价下方，空单在上方。
筛因子默认用跨币种比较，至少要 5 个币；少于 5 个时自动改用单币择时口径（时间序列 IC）。

## 5. 接入你的策略（3 步）

1. **写规格**：填 `docs/STRATEGY_TEMPLATE.md`，包括适用市场、入场信号、止损、仓位、离场、失效条件和对应的想法 id。
2. **改策略**：编辑 `user_data/strategies/MyStrategy.py`。
   - 设置 `timeframe`。
   - 在 `populate_entry_trend()` 里写 `enter_long = 1`（做多）或 `enter_short = 1`（做空）和 `fw_stop`（绝对止损价：多单在下方、空单在上方），按需写 `fw_entry_cap`（最差可接受的入场价）。允许的方向在配置里设（第 4 节）。
   - 按需实现 `take_profit_legs()`（分批止盈）和 `strategy_exit()`（策略自己的离场）。
   - **删掉 `is_template = True`**；改了类名就同步修改配置里的 `"strategy"`。
3. **验证**：依次运行 `pytest -q` → `s03ft research g0`（前视偏差检查）→ 研究矩阵 → 统计检验（第 6 节）。

接口说明在 `docs/FRAMEWORK.md`，可参考 `examples/strategies/SmaCrossExample.py` 的写法。

## 6. 从想法到上线的完整流程

```
登记想法 → 筛因子 → 写成策略 → G0 偏差检查 → 组合回测 + 研究矩阵 → 统计检验 → 你批准 → dry-run ≥ 4 周 → 你关闭 dry_run（小额）
 lab idea   lab factors          research g0    research portfolio/run  lab stats/matrix  idea move   s03-freqtrade
```

```bash
# ① 登记想法：假设、机制、证伪条件三项必填；回测之前登记样本外起点（只能登记一次）
s03ft lab idea add --title "…" --hypothesis "…" --mechanism "…" --falsification "…" --source human
s03ft lab idea holdout --id I-… --start 2025-06-01

# ② 筛因子（5 个币，跨币种比较；只有 alive / reversed 且 FDR 显著才算候选）
s03ft lab factors --tf 1d --horizon 3

# ③ 写成策略后：偏差检查 + 组合回测（主证据）+ 研究矩阵（诊断；逐笔 R 另存 research/results/trades/）
s03ft research g0 --timerange 20250101-
s03ft events build --out research/events_history.json --history   # 回测用的历史事件
s03ft research portfolio --all --idea I-… --trials base,stop_floor,fill_exit --timerange 20220101-   # 共享余额；base + 压力方案 + 候选方案（PBO 用）
s03ft research plan --idea I-… --pairs BTC,ETH,SOL,BNB,HYPE --years 2023-2025 --variants base,stop_floor,fill_exit,cost2x,delay1   # 诊断：按币、按年拆解
s03ft research run --job BTC-2024-base             # 对 research/jobs.json 里每一格执行
s03ft research aggregate                           # 只汇总清单里、当前版本、成功的任务

# ④ 上线判定（唯一入口）：组合回测 + 策略声明的全部压力方案 + 登记的样本外段 + PBO（组合回测候选，只用开发区间）
s03ft research verdict --idea I-…
s03ft lab matrix --idea I-…                        # 诊断：PBO、各方案对比

# ⑤ 批准（只能由你在终端执行；s03ft 会拒绝 --by human）
python -m fwresearch idea move --id I-… --to approved --reason "已审阅报告" --by human --approver <你的名字>
```

之后按 `deploy/README.md` 的上线前演练运行 dry-run 至少 4 周（G8），再由你决定是否关闭 dry_run（G9）。

## 7. 可选：接入 Vibe-Trading 找想法

[Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 提供大模型研究、多智能体讨论、读网页 / 文档和 478 个因子。本框架通过一个白名单代理（`mcp-guard`）把它接给 OpenClaw 研究 agent：

- 只放行 26 个研究工具；交易、连接器、写文件类工具一律拒绝；
- 不传任何交易所密钥；
- 每次调用写审计日志。

```bash
sudo bash deploy/install.sh --with-vibe-trading    # 另建 /opt/s03/vt-venv（约 1.1 GB），安装 0.1.18 并自动运行自检
sudo editor /etc/s03/vibe-trading.env              # 只填大模型配置（多智能体讨论需要；因子库和行情不需要）

s03ft lab vt-check                                 # 自检：版本、因子库桥接、MCP 握手、白名单、拒绝交易工具、密钥过滤
VT_PYTHON=/opt/s03/vt-venv/bin/python s03ft lab factors --zoo all   # 内置 55 个 + Vibe-Trading 408 个因子
```

然后在 OpenClaw 配置里把 `mcp.servers["vibe-trading"].enabled` 改为 `true`，运行 `openclaw mcp probe vibe-trading`，应显示 26 个工具。
完整的验证记录（哪些配置因实测而修改）见 `docs/RESEARCH_LAB.md` 的“安装验证记录”。

Vibe-Trading 只用于找想法和粗筛。它的回测按“下一根开盘调仓”成交、不模拟止损单；它的加密行情是 OKX 现货、按 UTC+8 切日。所以上线证据一律以 freqtrade 回测为准。

### 新闻事件守卫：Odaily + BlockBeats（v2.5）

只用来做两件事。一是**重大事件守卫**：按币种或交易所暂停新开仓，只收紧不放宽；守卫文件 30 分钟不更新就失效，不会卡住交易。二是**存档与历史回填**：作为候选因子去检验。不用新闻情绪调整仓位、杠杆或进出场。

两个来源同时接入，都是必需来源：Odaily 免费，历史可回填到 2018 年；BlockBeats 付费会员，另有 ETF 资金流、稳定币、持仓量等数据。一家某轮失败时守卫状态为 `degraded`，另一家的封禁照常生效并告警。v2.5 起事件规则**只看正文、不看标题**（逐句判断）；在调规则时没看过的 7 个月数据上，封禁时间占比最高 3.6%，但命中里误报占多数。实测对比、规则说明和偏差见 `docs/RESEARCH_LAB.md` 的“新闻来源：BlockBeats 与 Odaily”。

**上线前先回放**：用 `news backfill` 补历史，再用 `news replay` 看规则在过去会封多久、封的是不是真事件。规则与封禁时长都是示例值。

```bash
sudo bash deploy/install.sh --with-news           # Odaily + BlockBeats 采集与数据快照；--with-blockbeats 另装研究 MCP
sudo editor /etc/s03/blockbeats.env               # 只填 BLOCKBEATS_API_KEY（必填）
s03ft lab news backfill --sources odaily --since 2024-01-01   # 回填历史，再 replay 看误报
s03ft lab news replay --source odaily --since 2024-01-01 --verbose
s03ft status                                      # news_guard 显示 disabled / ok / stale 和当前封禁
```

## 8. 部署到服务器、交给 OpenClaw 运营

```bash
sudo bash deploy/install.sh [--with-vibe-trading]   # 可重复运行；不覆盖已有的密钥与配置
sudo editor /etc/s03/ft-trader.env                  # 填 Binance API Key（不开提现、限制 IP）
openclaw config patch --file /etc/s03/openclaw-templates/openclaw.s03.json5 --dry-run   # 先校验
openclaw config patch --file /etc/s03/openclaw-templates/openclaw.s03.json5             # 再合并
openclaw approvals set --file /etc/s03/openclaw-templates/exec-approvals.s03.json        # 命令白名单
openclaw doctor
sudo systemctl start s03-events s03-ft-gate s03-ft-monitor s03-freqtrade
sudo -u openclaw s03ft doctor
```

细节、4 个 agent 的分工和安全边界见 [`OPENCLAW.md`](OPENCLAW.md) 与 [`deploy/README.md`](deploy/README.md)。
分工原则：**agent 通过 `s03ft` 操作一切，交易决策只由策略完成，上线、恢复、改参数只由你操作。**

## 9. 日常命令速查

| 目的 | 命令 |
|---|---|
| 自检 / 状态 / 每日例行 | `s03ft doctor` / `s03ft status` / `s03ft daily` |
| 启停服务 | `s03ft svc start|stop|restart freqtrade|monitor|gate` |
| 暂停新开仓（恢复只能由你在 FreqUI 或 API 操作） | `s03ft gate pause "原因"` |
| 组合回测 / 研究矩阵 / 偏差检查 | `s03ft research portfolio` / `s03ft research plan|run|aggregate|verify` / `s03ft research g0` |
| 筛因子 / 统计检验 / 想法 | `s03ft lab factors` / `s03ft lab matrix` / `s03ft lab idea list` |
| Vibe-Trading 自检 | `s03ft lab vt-check` |
| 复盘指标 | `s03ft metrics` |
| 跑测试 | `s03ft test` 或 `pytest -q` |

## 10. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `s03ft doctor` 显示“仍是空白模板” | 正常：还没接入策略。接入后删掉 `is_template = True` |
| 下载数据返回 451 或 “restricted location” | 所在地区访问不了 Binance。本地研究用 `S03_FT_CONFIG=user_data/config-test-okx.json`；实盘服务器必须能访问 Binance |
| `lab factors` 报“缺少这些标的的数据：HYPE…” | 先下载：`freqtrade download-data -c $S03_FT_CONFIG --pairs HYPE/USDT:USDT --timeframes 1d --trading-mode futures` |
| 截面样本从 2025-02 才开始 | HYPE 永续 2025-02-21 才在 OKX 上市，跨币种比较只用 5 个币**同时**有数据的日期 |
| 因子显示 `insufficient` | IC 观测太少（截面 < 60、时间序列 < 12 个月）。资金费因子常见：OKX 只能下载约 3 个月资金费率 |
| 所有因子都是 dead | 不到两年的数据、5 个高度相关的币，本来就很难有显著结论。不要为了出结果去调阈值 |
| `lab matrix` 报“没有逐笔文件” | 先 `s03ft research plan` 再 `run --job …`；只读清单里、当前版本、成功的任务（被排除的会列在 stderr） |
| 统计结论总是“证据不足” | 笔数 < 100、没给 `--holdout-start`、缺压力方案或缺 DSR 数据时都会这样，按报告里列出的缺项补齐；不要降低门槛 |
| dry-run 一直不开仓，日志“事件日历不可用” | 实盘默认要求事件日历：先 `systemctl start s03-events`；本地研究副本可设 `S03_EVENTS_REQUIRED=0` |
| 交易以 `fw_invalid_stop:…` 离场 | 成交后止损无效（止损不在成交价的正确一侧——多单不低于、空单不高于成交价——超出上限、风险超预算或没有框架计划）：框架立即离场，原因写在日志 |
| 只有多单（或只有空单） | 看配置的 `fw_trade_direction` 与 `fw_pair_directions`；`s03ft doctor` 会列出当前设置 |
| `s03ft doctor` 提示配置写了 timeframe | 删掉配置里的 `timeframe`：它会覆盖策略周期（v2.5 及以前的配置写死了 1m） |
| 某个币突然不再开新仓，日志里有 `news_block` | 新闻事件守卫（Odaily / BlockBeats）命中了这个币或交易所的重大事件（时长见 `docs/RESEARCH_LAB.md`）。用 `lab news blocks` 查看原因和到期时间；不要为了恢复交易去改规则 |
| 守卫状态是 `stale` | 采集定时器 30 分钟没有更新（Key 失效、限额或网络）。交易程序已自动忽略守卫，修好采集后自动恢复 |
| `openclaw` 提示 Node 版本不支持 | 2026.10.8 需要 Node ≥ 24.22.1 或 ≥ 26.12.1 |
| `openclaw doctor` 提示沙箱会过滤 MCP 工具 | 用仓库里最新的 `openclaw.s03.json5`：研究 agent 已加 `tools.sandbox.tools.alsoAllow: ["vibe-trading__*"]` |
| `vt-check` 的 `mcp_tools.missing_from_upstream` 不为空 | Vibe-Trading 升级后改了工具名；回到 0.1.18，或同步更新 `fwresearch/mcp_guard.py` 的白名单并重跑测试 |

## 11. 文档地图

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 给 AI agent 的操作守则与任务命令 |
| [`OPENCLAW.md`](OPENCLAW.md) | 交给 OpenClaw 运营：组成、4 个 agent、落地步骤、安全边界 |
| [`docs/FRAMEWORK.md`](docs/FRAMEWORK.md) | 框架与策略的分工、`FrameworkStrategy` 接口 |
| [`docs/STRATEGY_TEMPLATE.md`](docs/STRATEGY_TEMPLATE.md) | 策略规格填写模板（含验证门槛 G0–G9） |
| [`docs/RESEARCH_LAB.md`](docs/RESEARCH_LAB.md) | 研究前端：流程、统计检验、因子口径、Vibe-Trading 安装验证记录 |
| [`docs/REFERENCE.md`](docs/REFERENCE.md) | 文件清单、已验证项、监控告警、已知问题 |
| [`docs/manual-freqtrade.md`](docs/manual-freqtrade.md) | 交易流程手册：框架保证与限制、验证门槛、执行与异常处理、版本记录 |
| [`deploy/README.md`](deploy/README.md) | 部署细节与上线前演练 |
| [`fwresearch/NOTICE.md`](fwresearch/NOTICE.md) | 移植代码的来源与许可（Vibe-Trading，MIT） |
