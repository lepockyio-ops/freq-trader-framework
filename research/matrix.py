"""研究矩阵与组合回测（freqtrade 版）。所有数字来自 freqtrade 输出。

  python research/matrix.py plan --pairs BTC,ETH,SOL,BNB,HYPE --years 2022-2025 --variants base,stop_floor,cost2x,delay1
  python research/matrix.py run --job SOL-2023-base
  python research/matrix.py aggregate                    # → research/matrix_report.md（只汇总清单里、当前版本、成功的任务）
  python research/matrix.py verify --job SOL-2023-base   # 重跑并比对逐笔哈希（含盈亏、费用、资金费、止损、订单）
  python research/matrix.py portfolio --variant base --timerange 20220101-   # 五币同时、连续时间、共享余额（主证据）
  python research/matrix.py g0 --timerange 20250101-     # lookahead-analysis + recursive-analysis
  s03ft lab matrix --holdout-start 2025-01-01            # 统计检验（docs/RESEARCH_LAB.md）

两种回测的用途不同（v2.6）：
- 研究矩阵：单币 × 单年，便于并行和拆解“哪个币、哪一年”贡献了结果。它测不到组合约束（同时持仓数、总风险、相关性、
  共享余额），年末会强制平仓，合并后的逐笔记录也不是同一个账户能走出的路径——只作诊断。
- 组合回测（portfolio）：白名单全部交易对、连续时间、一个账户。上线证据以它为准。

可复核性（v2.6）：
- plan 写入任务清单 research/jobs.json 和“版本指纹”（框架基类、策略文件、配置、本脚本、历史事件文件、数据文件清单、
  相关环境变量的哈希）。run 先删掉这个任务的旧结果再跑，并记录当时的指纹；aggregate 与 lab matrix 只用清单里、
  状态成功、指纹一致的任务（旧实验和失败重跑的残留不会混进来）。
- freqtrade 的回测缓存只看配置、策略参数和策略文件，不包含框架基类和外部文件，所以一律加 --cache none。

方案（只改变一个因素）：
  base        策略声明的参数（框架默认值 + 策略的 framework_params）
  stop_floor  止损太近时改为下限，而不是放弃
  cost2x      每边手续费 0.19%（0.05% × 2 + 滑点 0.03% × 3 折算成手续费，示例值）
  delay1      入场信号推迟一根 K 线（只在回测里生效），检验对执行延迟的敏感度
  fill_exit / fill_keep  成交价更优、止损太近时立即离场 / 保留原止损（与 widen 对比）
方案的参数通过 S03_RESEARCH_OVERRIDES 传给框架，优先级高于策略的 framework_params，只在回测里生效（v2.8）。
方案与 base 的实际参数相同（例如策略本身就用 floor）时，这个方案记为失败，不出结果——不会悄悄跑成 base 的复制品。
上线判定的 PBO 与尝试次数来自组合回测的候选方案（portfolio --all --trials …，只用样本外起点之前的开发区间，v2.10）；
研究矩阵只作诊断。止损额外滑点由 stopslip 在 base 的逐笔记录上事后近似（含框架的 fw_stop_crossed、fw_invalid_stop）。
逐笔 R 的分母来自框架在回测里写下的初始风险记录（每次运行的 risk.jsonl，v2.11）；缺记录的交易 R 记为未知，上线判定最多“证据不足”。
未实现（freqtrade 回测不支持，结论里要写明）：跳空、资金费 × 2、未成交与排队。

环境变量：S03_FT_CONFIG（默认 user_data/config.json）、S03_FT_EXTRA_CONFIG（本机专属的附加配置，如代理证书，不进仓库）、
          FW_STRATEGY、FW_STRATEGY_PATH、S03_RESEARCH_EVENTS（历史事件文件，默认 research/events_history.json，
          由 s03ft events --history --out research/events_history.json 生成）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # fwresearch（想法登记簿）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
import trade_risk  # noqa: E402  逐笔初始风险与 R：研究与复盘同一口径（v2.10）

ROOT = Path("research")
RES = ROOT / "results"
CONFIG = os.environ.get("S03_FT_CONFIG", "user_data/config.json")
EVENTS = Path(os.environ.get("S03_RESEARCH_EVENTS", str(ROOT / "events_history.json")))
VARIANTS = {                                    # 方案 → (框架参数覆盖, freqtrade 额外参数)
    "base": ({}, []),
    "stop_floor": ({"stop_min_mode": "floor"}, []),
    "cost2x": ({}, ["--fee", "0.0019"]),
    "delay1": ({"research_entry_delay_bars": 1}, []),
    "fill_exit": ({"better_fill_policy": "exit"}, []),        # 成交价更优、止损太近时立即离场（与 widen 对比）
    "fill_keep": ({"better_fill_policy": "keep"}, []),        # 保留原结构止损
}
FINGERPRINT_ENV = ("FW_STRATEGY", "FW_STRATEGY_PATH", "S03_FT_CONFIG", "S03_FT_EXTRA_CONFIG", "S03_RESEARCH_EVENTS")
RUN_VARIANTS = {"cost2x", "delay1"}            # 需要单独回测的压力方案
POST_HOC = {"stopslip"}                         # 由 base 的逐笔记录事后计算的压力方案
PORTFOLIO = RES / "portfolio"


def strategy_name() -> str:
    name = os.environ.get("FW_STRATEGY")
    if not name:
        try:
            name = json.loads(Path(CONFIG).read_text("utf-8")).get("strategy")
        except (OSError, ValueError):
            name = None
    return name or "MyStrategy"


def strategy_args() -> list[str]:
    """策略名：环境变量 FW_STRATEGY，否则读配置文件里的 "strategy"；FW_STRATEGY_PATH 可指向 examples/strategies。"""
    args = ["--strategy", strategy_name()]
    if os.environ.get("FW_STRATEGY_PATH"):
        args += ["--strategy-path", os.environ["FW_STRATEGY_PATH"]]
    return args


def config_args() -> list[str]:
    extra = ["-c", os.environ["S03_FT_EXTRA_CONFIG"]] if os.environ.get("S03_FT_EXTRA_CONFIG") else []
    return ["-c", CONFIG, *extra]


def _sha(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        h.update(p.encode())
        try:
            h.update(Path(p).read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


_RESOLVED: dict = {}


def resolved_config() -> tuple[str, dict]:
    """freqtrade 解析后的最终配置（含 S03_FT_EXTRA_CONFIG 与全部 FREQTRADE__… 环境变量覆盖；密钥由 freqtrade 显示为 REDACTED）。
    返回（原文, 解析后的字典）；同一进程只调用一次 freqtrade。"""
    if "body" not in _RESOLVED:
        r = subprocess.run([freqtrade_bin(), "show-config", *config_args()], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise SystemExit(f"freqtrade show-config 失败：{r.stderr[-500:]}")
        body = r.stdout[r.stdout.find("{"):]
        try:
            cfg = json.JSONDecoder().raw_decode(body)[0]
        except ValueError:
            cfg = {}
        _RESOLVED.update(body=body, cfg=cfg)
    return _RESOLVED["body"], _RESOLVED["cfg"]


def resolved_config_hash() -> str:
    return hashlib.sha256(resolved_config()[0].encode()).hexdigest()[:16]


def exchange_and_pairs() -> tuple[str, list[str]]:
    """最终配置里的交易所与白名单（与回测实际使用的一致）；读不到时退回配置文件。"""
    try:
        cfg = resolved_config()[1]
        return cfg["exchange"]["name"], list(cfg["exchange"]["pair_whitelist"])
    except (KeyError, TypeError, SystemExit, OSError, subprocess.SubprocessError):
        cfg = json.loads(Path(CONFIG).read_text("utf-8"))
        return cfg["exchange"]["name"], list(cfg["exchange"]["pair_whitelist"])


def data_hash(data_dir: Path = Path("user_data/data")) -> str:
    """数据文件的内容校验和（v2.7；v2.6 只看大小和修改时间）。按（路径, 大小, 修改时间）缓存每个文件的 sha256，避免重复计算。"""
    cache_p = ROOT / ".data_sha256_cache.json"
    try:
        cache = json.loads(cache_p.read_text("utf-8"))
    except (OSError, ValueError):
        cache = {}
    h, new = hashlib.sha256(), {}
    for f in sorted(data_dir.rglob("*.feather")) if data_dir.exists() else []:
        st = f.stat()
        key = f"{f}:{st.st_size}:{st.st_mtime_ns}"
        digest = cache.get(key) or hashlib.sha256(f.read_bytes()).hexdigest()
        new[key] = digest
        h.update(f"{f.relative_to(data_dir)}:{digest}".encode())
    try:
        ROOT.mkdir(exist_ok=True)
        cache_p.write_text(json.dumps(new), "utf-8")
    except OSError:
        pass
    return h.hexdigest()[:16]


REPO = Path(__file__).resolve().parents[1]


def repo_modules() -> list[Path]:
    """本进程已导入、文件在仓库里的模块（相对路径）：以后新增的研究依赖自动进入版本指纹。"""
    out = []
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if not f:
            continue
        try:
            rel = Path(f).resolve().relative_to(REPO)
        except ValueError:
            continue
        if rel.suffix == ".py" and "tests" not in rel.parts:
            out.append(rel)
    return out


def fingerprint_files() -> list[str]:
    """版本指纹覆盖的代码文件（固定清单，不随本次导入了什么而变）。v2.11 补上 monitor/trade_risk.py（外部审查 v2.10 P1-3）；
    测试 test_fingerprint_covers_every_imported_repo_module 检查研究流程导入的仓库内模块都在清单里。"""
    code = [Path("user_data/strategies/framework_base.py"), Path("research/matrix.py"), Path(CONFIG), Path("monitor/trade_risk.py")]
    code += list(Path("user_data/strategies").glob("*.py"))
    if os.environ.get("FW_STRATEGY_PATH"):
        code += list(Path(os.environ["FW_STRATEGY_PATH"]).glob("*.py"))
    code += list(Path("fwresearch").rglob("*.py"))
    return sorted({Path(os.path.normpath(str(c))).as_posix() for c in code})   # v2.12：正斜杠，跨平台指纹一致


def fingerprint() -> dict:
    """结果属于哪个版本：代码、freqtrade 解析后的最终配置、依赖版本、事件文件、数据内容、相关环境变量。
    任何一项变了，旧结果就不再被汇总或判定。"""
    import platform

    import freqtrade
    import numpy
    import pandas

    code = fingerprint_files()
    parts = {"code": _sha(code), "config": resolved_config_hash(), "events": _sha([EVENTS]) if EVENTS.exists() else "none",
             "data": data_hash(),
             "versions": f"freqtrade {freqtrade.__version__}; python {platform.python_version()}; pandas {pandas.__version__}; numpy {numpy.__version__}",
             "env": hashlib.sha256(json.dumps({k: os.environ.get(k) for k in FINGERPRINT_ENV}, sort_keys=True).encode()).hexdigest()[:16]}
    parts["id"] = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]
    return parts


def strategy_requirements() -> dict:
    """读策略类声明的研究要求（research_stress、research_stop_slip）、周期，以及 framework_params 里研究方案会改的参数。"""
    import importlib.util

    name = strategy_name()
    dirs = [Path("user_data/strategies")] + ([Path(os.environ["FW_STRATEGY_PATH"])] if os.environ.get("FW_STRATEGY_PATH") else [])
    sys.path[:0] = [str(Path("user_data/strategies").resolve())] + [str(d.resolve()) for d in dirs]
    for d in dirs:
        for f in sorted(d.glob("*.py")):
            if f"class {name}(" not in f.read_text("utf-8", errors="ignore"):
                continue
            spec = importlib.util.spec_from_file_location(f"_s03_strategy_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = getattr(mod, name)
            import framework_base as fb

            req = tuple(getattr(cls, "research_stress", fb.DEFAULT_RESEARCH_STRESS))
            unknown = set(req) - RUN_VARIANTS - POST_HOC
            if unknown:
                raise SystemExit(f"{name}.research_stress 含未知压力方案 {sorted(unknown)}；可选 {sorted(RUN_VARIANTS | POST_HOC)}")
            below = sorted(set(fb.MIN_RESEARCH_STRESS) - set(req))
            if below:                                      # v2.9：框架的最低压力方案，策略不能去掉
                raise SystemExit(f"{name}.research_stress 去掉了框架要求的压力方案 {below}（最低集合 {list(fb.MIN_RESEARCH_STRESS)}）")
            waivers = dict(getattr(cls, "research_stress_waivers", None) or {})
            dropped = [v for v in fb.DEFAULT_RESEARCH_STRESS if v not in req]
            no_reason = [v for v in dropped if len(str(waivers.get(v) or "").strip()) < 4]
            if no_reason:
                raise SystemExit(f"{name} 去掉了默认压力方案 {no_reason}，但 research_stress_waivers 里没有写理由")
            fp_ = {k: v for k, v in (getattr(cls, "framework_params", None) or {}).items() if k in fb.RESEARCH_KEYS}
            all_fw = {**fb.FW_DEFAULTS, **(getattr(cls, "framework_params", None) or {})}
            return {"strategy": name, "required_stress": list(req),
                    "stress_waivers": {v: str(waivers[v]) for v in dropped},
                    "stop_slip": float(getattr(cls, "research_stop_slip", 0.002)),
                    "timeframe": getattr(cls, "timeframe", None),
                    "strategy_file_sha": hashlib.sha256(f.read_bytes()).hexdigest()[:16],
                    "research_params": {**{k: fb.FW_DEFAULTS[k] for k in fb.RESEARCH_KEYS}, **fp_},
                    "risk_params": {"stop_min_pct": float(all_fw["stop_min_pct"]), "stop_max_pct": float(all_fw["stop_max_pct"])}}
    raise SystemExit(f"找不到策略类 {name}（user_data/strategies 或 FW_STRATEGY_PATH）")


def effective_params(variant: str, req: dict) -> dict:
    """这个方案实际生效的研究参数（策略的值 + 方案覆盖）与 freqtrade 额外参数，写进运行记录。"""
    ov, args = VARIANTS[variant]
    return {**req["research_params"], **ov, "freqtrade_args": list(args)}


def variant_problem(variant: str, req: dict) -> str | None:
    """方案与 base 实际参数相同 → 返回原因（外部审查 v2.7 P2：策略的 framework_params 把研究方案覆盖掉，方案等于没跑）。"""
    if variant == "base":
        return None
    if effective_params(variant, req) == effective_params("base", req):
        return (f"方案 {variant} 与 base 的实际参数相同（策略的 framework_params 已经是 {VARIANTS[variant][0]}）："
                "这个方案没有意义，不运行")
    return None


def trial_key(variant: str, req: dict, fp: dict) -> str:
    """参数组合标识（v2.9）：策略文件内容 + 最终配置 + 这个方案实际生效的参数。原样重跑得到同一个值，不会虚增尝试次数。"""
    key = {"strategy": req["strategy"], "strategy_file": req.get("strategy_file_sha"), "config": fp.get("config"),
           "params": {k: v for k, v in effective_params(variant, req).items() if k != "freqtrade_args"}}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


IDEAS = Path(os.environ.get("FW_IDEAS_FILE", str(ROOT / "ideas.jsonl")))


def check_idea(idea: str) -> str:
    """研究运行必须属于一个已登记的想法（v2.9）。"""
    from fwresearch.ideas import IdeaBook

    if not idea:
        raise SystemExit("必须用 --idea 指定想法 id：每次回测都会记入想法登记簿（样本外须在第一次回测之前登记）")
    try:
        IdeaBook(IDEAS).get(idea)
    except KeyError:
        raise SystemExit(f"想法登记簿 {IDEAS} 里没有 {idea}：先 lab idea add") from None
    return idea


def record_backtest(idea: str, variant: str, req: dict, fp: dict, **info) -> None:
    """每次开始回测时写进想法登记簿（只追加）。外部审查 v2.8 P1-1：v2.8 的研究脚本不写登记簿，看完结果再登记样本外、
    原样重跑一次，判定就认为是事先登记。"""
    from fwresearch.ideas import IdeaBook

    IdeaBook(IDEAS).record_backtest(idea, {"variant": variant, "stress": variant in RUN_VARIANTS,
                                           "trial_key": trial_key(variant, req, fp), "fingerprint": fp.get("id"), **info})


def experiment_id(fp_id: str, req: dict, timerange: str, pairs: list[str], idea: str = "") -> str:
    """实验标识（v2.8；v2.9 加入想法 id）：版本指纹 + 想法 + 策略 + 必需压力方案 + 时间段 + 交易对。上线判定只采用同一实验的运行。"""
    key = {"fingerprint": fp_id, "strategy": req["strategy"], "required_stress": sorted(req["required_stress"]),
           "stop_slip": req.get("stop_slip"), "timerange": timerange, "pairs": sorted(pairs), "idea": idea}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def parse_job(jid: str):
    base, year, variant = jid.split("-", 2)
    if variant not in VARIANTS:
        raise ValueError(f"无效方案：{variant}")
    return base, int(year), variant


def _trade_dict(row) -> dict:
    return row.to_dict() if hasattr(row, "to_dict") else dict(row)


def r_multiple(row, records: dict | None = None, risk_params: dict | None = None) -> float | None:
    """逐笔 R：与复盘指标同一口径（monitor/trade_risk.py）。给了 records（回测旁路文件，v2.11）时只用记录的初始风险，
    缺记录即未知；没给时按 risk_params（策略实际的止损上下限）事后推算。"""
    return trade_risk_of(_trade_dict(row), records, risk_params)[0]


def trade_risk_of(t: dict, records: dict | None, risk_params: dict | None) -> tuple[float | None, str]:
    """（R, 风险来源）。"""
    kw = dict(risk_params or {})
    if records is not None:
        rec = records.get(trade_risk.record_key(t.get("pair"), t.get("open_date")))
        kw.update(recorded=rec, require_record=True)
    return trade_risk.r_multiple(t, **kw)


def trades_frame(jid: str, trades, records: dict | None = None, risk_params: dict | None = None) -> "pd.DataFrame":
    import pandas as pd

    base, year, variant = parse_job(jid)
    cols = ["job", "coin", "year", "variant", "entry_time", "time", "R", "pnl", "risk", "risk_source", "is_short"]
    if trades is None or len(trades) == 0:
        return pd.DataFrame(columns=cols)
    rows = [_trade_dict(r) for _, r in trades.iterrows()]
    risk = [risk_of(r, records, risk_params) for r in rows]
    out = pd.DataFrame({"job": jid, "coin": base, "year": year, "variant": variant,
                        "entry_time": trades["open_date"].values, "time": trades["close_date"].values,
                        "R": [float(r["profit_abs"]) / rk if rk else None for r, (rk, _) in zip(rows, risk)],
                        "pnl": trades["profit_abs"].values, "risk": [rk for rk, _ in risk], "risk_source": [s_ for _, s_ in risk],
                        "is_short": [bool(r.get("is_short")) for r in rows]})
    return out[cols]


def risk_of(t: dict, records: dict | None, risk_params: dict | None) -> tuple[float | None, str]:
    """（初始风险, 来源）：给了 records（回测旁路文件）时只用记录，缺记录即未知。"""
    kw = dict(risk_params or {})
    if records is not None:
        kw.update(recorded=records.get(trade_risk.record_key(t.get("pair"), t.get("open_date"))), require_record=True)
    return trade_risk.initial_risk(t, **kw)


def result_hash(tf) -> str:
    """研究结果的哈希（v2.12，外部审查 v2.11 P2-2）：按（标的, 入场时间, 平仓时间）排序后的初始风险、R、风险来源与净盈亏。
    trades_hash 只证明 freqtrade 的成交一致；这个哈希证明最终进入统计的 R 也一致。"""
    if tf is None or len(tf) == 0:
        return hashlib.sha256(b"").hexdigest()[:16]

    def num(x, nd):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return None if v != v else round(v, nd)

    rows = sorted([str(r["coin"]), str(r["entry_time"]), str(r["time"]), num(r.get("risk"), 8), num(r.get("R"), 8),
                   str(r.get("risk_source")), num(r.get("pnl"), 8)] for _, r in tf.iterrows())
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()[:16]


def compare_runs(stored: dict, fresh: dict) -> dict:
    """复核结论：成交、最终 R 与版本都一致且没有 R 未知 → 完整复现；成交一致但有 R 未知 → 风险证据不完整（不算复现成功）。"""
    ok = stored.get("status") == "ok" and fresh.get("status") == "ok" and stored.get("trades_hash") and fresh.get("trades_hash")
    same_trades = bool(ok) and stored.get("trades_hash") == fresh.get("trades_hash")
    same_result = bool(ok) and stored.get("result_hash") is not None and stored.get("result_hash") == fresh.get("result_hash")
    same_version = stored.get("fingerprint") == fresh.get("fingerprint")
    unknown = max(int(stored.get("n_r_unknown") or 0), int(fresh.get("n_r_unknown") or 0))
    if same_trades and same_result and same_version and not unknown:
        status = "完整复现"
    elif same_trades and same_version and unknown:
        status = "成交一致、风险证据不完整"
    else:
        status = "不一致"
    return {"match": status == "完整复现", "status": status, "same_trades": same_trades, "same_result": same_result,
            "same_version": same_version, "n_r_unknown": {"stored": stored.get("n_r_unknown"), "fresh": fresh.get("n_r_unknown")},
            "trades_hash": {"stored": stored.get("trades_hash"), "fresh": fresh.get("trades_hash")},
            "result_hash": {"stored": stored.get("result_hash"), "fresh": fresh.get("result_hash")}}


ORDER_HASH_FIELDS = ("ft_order_side", "ft_is_entry", "order_type", "status", "amount", "filled", "remaining", "safe_price",
                     "average", "cost", "ft_order_tag", "order_timestamp", "order_filled_timestamp", "funding_fee")
HASH_COLS = ("pair", "open_date", "close_date", "open_rate", "close_rate", "amount", "profit_abs", "fee_open",
             "fee_close", "funding_fees", "stop_loss_abs", "exit_reason", "enter_tag", "leverage")


def trades_hash(trades) -> str:
    """复核用哈希：逐笔的价格、数量、净盈亏、费用、资金费、止损、离场原因，以及每笔的全部订单。"""
    if trades is None or len(trades) == 0:
        return hashlib.sha256(b"").hexdigest()[:16]
    cols = [c for c in HASH_COLS if c in trades.columns]
    rows = []
    for _, r in trades.iterrows():
        rec = {c: str(r[c]) for c in cols}
        rec["orders"] = [{k: o.get(k) for k in ORDER_HASH_FIELDS} for o in (r.get("orders") or [])]
        rows.append(rec)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]


def freqtrade_bin() -> str:
    return shutil.which("freqtrade") or "freqtrade"


def research_env(variant: str, risk_log: Path | None = None) -> dict:
    overrides, _ = VARIANTS[variant]
    env = {**os.environ, "S03_RESEARCH_OVERRIDES": json.dumps(overrides) if overrides else "",   # base 不继承外部环境
           "S03_RISK_LOG": str(risk_log) if risk_log else "",                         # 每笔初始风险的旁路记录（v2.11）
           "S03_EVENTS_REQUIRED": "0", "S03_NEWS_FILE": "",                           # 新闻守卫没有历史，回测不用
           "S03_EVENTS_FILE": str(EVENTS) if EVENTS.exists() else ""}
    return env


def run_freqtrade(variant: str, timerange: str, pairs: list[str] | None, outdir: Path) -> tuple[dict, object]:
    _, args = VARIANTS[variant]
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [freqtrade_bin(), "backtesting", *config_args(), *strategy_args(), "--timerange", timerange,
           "--enable-protections", "--cache", "none", "--export", "trades", "--backtest-directory", str(outdir), *args]
    if pairs:
        cmd += ["-p", *pairs]
    risk_log = outdir / "risk.jsonl"
    risk_log.unlink(missing_ok=True)
    r = subprocess.run(cmd, env=research_env(variant, risk_log), capture_output=True, text=True)
    info = {"cmd": " ".join(cmd), "events_file": str(EVENTS) if EVENTS.exists() else None, "risk_log": str(risk_log)}
    if r.returncode != 0:
        return {**info, "status": "error", "stderr_tail": r.stderr[-1500:]}, None
    from freqtrade.data.btanalysis import load_backtest_data

    return {**info, "status": "ok"}, load_backtest_data(outdir)


def backtest(jid: str, outdir: Path, req: dict | None = None, idea: str = "") -> dict:
    base, year, variant = parse_job(jid)
    req = req or strategy_requirements()
    fp = fingerprint()
    out = {"job": jid, "pair": base, "year": year, "variant": variant, "fingerprint": fp["id"], "idea": idea,
           "effective_params": effective_params(variant, req), "trial_key": trial_key(variant, req, fp)}
    problem = variant_problem(variant, req)
    if problem:
        return {**out, "status": "error", "error": problem}
    if idea:
        record_backtest(idea, variant, req, fp, source="matrix", job=jid)
    info, trades = run_freqtrade(variant, f"{year}0101-{year + 1}0101", [f"{base}/USDT:USDT"], outdir)
    out.update(info)
    if trades is None:
        return out
    n = int(len(trades))
    prof = float(trades["profit_abs"].sum()) if n else 0.0
    win = float((trades["profit_abs"] > 0).mean()) if n else None
    records = trade_risk.load_records(info.get("risk_log"))
    tf = trades_frame(jid, trades, records, req.get("risk_params"))
    return {"_trades": tf, **out, "n_trades": n, "profit_abs": round(prof, 4), "risk_params": req.get("risk_params"),
            "n_r_unknown": int(tf["R"].isna().sum()) if len(tf) else 0, "result_hash": result_hash(tf),
            "win_rate": None if win is None else round(win, 4), "trades_hash": trades_hash(trades)}


def equity_stats(pnl_by_close, start_equity: float) -> dict:
    """按平仓顺序的已实现权益曲线：最大回撤（不含持仓期间的浮亏，结论里要写明）。"""
    eq, peak, mdd = start_equity, start_equity, 0.0
    for p in pnl_by_close:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
    return {"final_equity": round(eq, 2), "max_drawdown_closed": round(mdd, 4)}


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), "utf-8")
    os.replace(tmp, path)


def portfolio(variant: str, timerange: str, fp: dict | None = None, req: dict | None = None, idea: str = "") -> dict:
    """组合回测一个方案。每次运行一个独立目录 run-<时间>。
    v2.8（外部审查 v2.7 P1-4）：开始时先把 latest.json 写成 running，任何异常（包括读结果、算统计、写文件出错）
    都写成 error——latest 永远指向这一次运行，不会留下上一次成功的旧结果。运行记录带实验标识与开始时间。"""
    started = time.time()
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started)) + f"-{os.getpid()}"
    vdir = PORTFOLIO / variant
    rdir = vdir / f"run-{run_id}"
    rdir.mkdir(parents=True, exist_ok=True)
    res: dict = {"kind": "portfolio", "variant": variant, "timerange": timerange, "run_id": run_id, "idea": idea,
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(started)), "status": "running"}
    _write_atomic(vdir / "latest.json", res)
    try:
        _portfolio_body(res, variant, timerange, fp, req, rdir, idea)
    except BaseException as e:                       # noqa: BLE001  包括 SystemExit / KeyboardInterrupt：先记失败再抛出
        res.update(status="error", error=f"{type(e).__name__}: {str(e)[-500:]}")
        _write_atomic(rdir / "run.json", res)
        _write_atomic(vdir / "latest.json", res)
        if not isinstance(e, Exception):
            raise
        return res
    _write_atomic(rdir / "run.json", res)
    _write_atomic(vdir / "latest.json", res)                 # 原子替换：失败的运行也会成为 latest（状态 error）
    return res


def _portfolio_body(res: dict, variant: str, timerange: str, fp: dict | None, req: dict | None, rdir: Path,
                    idea: str = "") -> None:
    import pandas as pd

    fp = fp or fingerprint()
    req = req or strategy_requirements()
    _, pairs = exchange_and_pairs()
    res.update(fingerprint=fp["id"], fingerprint_parts=fp, strategy=req["strategy"], pairs=pairs,
               experiment_id=experiment_id(fp["id"], req, timerange, pairs, idea), effective_params=effective_params(variant, req),
               trial_key=trial_key(variant, req, fp))
    problem = variant_problem(variant, req)
    if problem:
        res.update(status="error", error=problem)
        return
    if idea:
        record_backtest(idea, variant, req, fp, source="portfolio", run_id=res["run_id"], timerange=timerange)
    info, trades = run_freqtrade(variant, timerange, None, rdir / "freqtrade")
    res.update(info)
    if trades is not None:
        try:
            wallet = float(resolved_config()[1].get("dry_run_wallet") or 10_000)
        except (TypeError, ValueError, SystemExit):
            wallet = 10_000.0
        t = trades.sort_values("close_date")
        rows = [_trade_dict(r) for _, r in t.iterrows()]
        ent_amt = [trade_risk.entry_filled_amount(r) for r in rows]
        records = trade_risk.load_records(info.get("risk_log"))      # v2.11：成交时记录的初始风险；缺记录 → 未知
        risk = [risk_of(r, records, req.get("risk_params")) for r in rows]
        tf = pd.DataFrame({"job": f"portfolio-{variant}", "coin": t["pair"].str.split("/").str[0].values, "year": 0,
                           "variant": variant, "entry_time": t["open_date"].values, "time": t["close_date"].values,
                           "R": [float(r["profit_abs"]) / rk if rk else None for r, (rk, _) in zip(rows, risk)],
                           "pnl": t["profit_abs"].values, "exit_reason": t["exit_reason"].values,
                           "notional": [a * float(r["open_rate"]) for a, r in zip(ent_amt, rows)],
                           "risk": [rk for rk, _ in risk], "risk_source": [src for _, src in risk],
                           "is_short": [bool(r.get("is_short")) for r in rows]})
        csv = rdir / "trades.csv"
        tf.to_csv(csv, index=False)
        res.update(n_trades=int(len(t)), profit_abs=round(float(t["profit_abs"].sum()), 4) if len(t) else 0.0,
                   by_coin=tf.groupby("coin")["pnl"].agg(["count", "sum"]).round(4).to_dict("index"),
                   max_concurrent=_max_concurrent(t), trades_hash=trades_hash(trades),
                   **equity_stats(t["profit_abs"].tolist(), wallet),
                   exits=t["exit_reason"].value_counts().to_dict(),
                   by_side={("short" if k else "long"): v for k, v in tf.groupby("is_short")["pnl"].agg(["count", "sum"]).round(4)
                            .to_dict("index").items()},
                   risk_sources=tf["risk_source"].value_counts().to_dict(), risk_params=req.get("risk_params"),
                   n_r_unknown=int(tf["R"].isna().sum()), result_hash=result_hash(tf),
                   risk_log_sha256=hashlib.sha256(Path(info["risk_log"]).read_bytes()).hexdigest()
                   if info.get("risk_log") and Path(info["risk_log"]).exists() else None,
                   csv=f"{rdir.name}/trades.csv", csv_sha256=hashlib.sha256(csv.read_bytes()).hexdigest(),
                   data_gaps=data_gaps(timerange, req.get("timeframe")))
        if not EVENTS.exists():
            res["data_gaps"].append("没有历史事件文件：回测里没有事件窗口（实盘会在事件前 2 小时到后 1 小时拒绝开仓）")


# 数据缺口判定（v2.8，示例值）：(最长允许的缺口 = 周期 × 倍数, 至少多长才算, 缺失比例上限)
GAP_RULES = {"candles": (2.0, "4h", 0.005), "mark": (2.0, "4h", 0.005), "funding": (1.5, "0h", 0.0), "daily": (1.5, "0h", 0.0)}


def _tf_delta(tf: str):
    import pandas as pd

    unit = {"m": "min", "h": "h", "d": "D", "w": "W"}[tf[-1]]
    return pd.Timedelta(int(tf[:-1]), unit=unit)


def data_gaps(timerange: str, timeframe: str | None = None, data_dir: Path = Path("user_data/data"),
              exchange: str | None = None, pairs: list[str] | None = None) -> list[str]:
    """研究数据缺口（v2.8，外部审查 v2.7 P1-6）：对最终配置里的每个交易对，检查主周期 K 线、标记价、资金费率、日线
    是否覆盖回测区间的**起点和终点**，以及区间内部有没有超过 GAP_RULES 的缺口。
    v2.7 只查起始日期：中间断档、提前结束、某个币资金费缺几天都查不到（freqtrade 在缺资金费的时段按 0 计算，不会报错）。"""
    import pandas as pd

    gaps: list[str] = []
    if exchange is None or pairs is None:
        try:
            exchange, pairs = exchange_and_pairs()
        except (OSError, ValueError, KeyError):
            return ["读不出配置里的交易所与交易对"]
    lo_s, _, hi_s = timerange.partition("-")
    lo = pd.Timestamp(lo_s, tz="UTC") if lo_s else None
    hi = pd.Timestamp(hi_s, tz="UTC") if hi_s else None
    d = data_dir / exchange / "futures"
    kinds = [("资金费率", "funding", "*-funding_rate.feather", None), ("日线", "daily", "-1d-futures.feather", "1d"),
             ("标记价", "mark", "*-mark.feather", None)]
    if timeframe:
        kinds.insert(0, (f"{timeframe} K 线", "candles", f"-{timeframe}-futures.feather", timeframe))
    ends: dict[str, dict[str, pd.Timestamp]] = {}
    for p in pairs:
        base = p.replace("/", "_").replace(":", "_")
        for label, rule, suffix, tf in kinds:
            files = sorted(d.glob(base + suffix)) if "*" in suffix else [d / (base + suffix)]
            files = [f for f in files if f.exists()]
            if not files:
                gaps.append(f"{p} 没有{label}数据")
                continue
            try:
                dates = pd.to_datetime(pd.read_feather(files[0], columns=["date"])["date"], utc=True).sort_values()
            except Exception as e:                         # noqa: BLE001
                gaps.append(f"{p} 的{label}读取失败：{type(e).__name__}")
                continue
            if dates.empty:
                gaps.append(f"{p} 的{label}是空文件")
                continue
            step = _tf_delta(tf) if tf else (dates.diff().dropna().median() if len(dates) > 2 else pd.Timedelta("8h"))
            mult, min_gap, max_missing = GAP_RULES[rule]
            first, last = dates.iloc[0], dates.iloc[-1]
            if lo is not None and first > lo + step:
                gaps.append(f"{p} 的{label}从 {first:%Y-%m-%d %H:%M} 才有（回测从 {lo:%Y-%m-%d} 开始；上市晚或数据缺失）")
            if hi is not None and last + step < hi - step * 0.5:
                gaps.append(f"{p} 的{label}到 {last:%Y-%m-%d %H:%M} 就没有了（回测到 {hi:%Y-%m-%d}）")
            ends.setdefault(label, {})[p] = last + step
            w = dates[(dates >= (lo or first)) & (dates <= (hi or last))]
            if len(w) >= 2:
                diff = w.diff().dropna()
                limit = max(step * mult, pd.Timedelta(min_gap))
                big = diff[diff > limit]
                expected = (w.iloc[-1] - w.iloc[0]) / step
                missing = float(((diff / step).round() - 1).clip(lower=0).sum() / expected) if expected > 0 else 0.0
                if len(big) or missing > max_missing:
                    worst = diff.max()
                    at = w.iloc[int(diff.to_numpy().argmax()) + 1] if len(diff) else w.iloc[-1]
                    gaps.append(f"{p} 的{label}区间内有缺口：最长 {worst}（截至 {at:%Y-%m-%d %H:%M}），"
                                f"超过 {limit} 的 {len(big)} 处，缺失约 {missing:.2%}")
    if hi is None:                                          # 开放的终点：与其他交易对比，提前结束的也算缺口
        for label, per in ends.items():
            latest = max(per.values())
            for p, e in per.items():
                if latest - e > pd.Timedelta("1D"):
                    gaps.append(f"{p} 的{label}比其他交易对早结束（到 {e:%Y-%m-%d}，其他到 {latest:%Y-%m-%d}）")
    return gaps


def _max_concurrent(t) -> int:
    ev = sorted([(o, 1) for o in t["open_date"]] + [(c, -1) for c in t["close_date"]], key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def load_manifest() -> dict:
    p = ROOT / "jobs.json"
    m = json.loads(p.read_text("utf-8"))
    return m if isinstance(m, dict) else {"jobs": m, "fingerprint": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--pairs", default="BTC,ETH,SOL,BNB,HYPE")
    p.add_argument("--years", default="2022-2025")
    p.add_argument("--variants", default="base,stop_floor,cost2x,delay1")
    p.add_argument("--idea", default="", help="想法 id（必填）：之后每次 run 都记入想法登记簿")
    for n in ("run", "verify"):
        sub.add_parser(n).add_argument("--job", required=True)
    pv = sub.add_parser("portfolio-verify", help="重跑一次组合回测并核对成交、最终 R 与版本（不改 latest）")
    pv.add_argument("--variant", default="base")
    sub.add_parser("aggregate")
    pf = sub.add_parser("portfolio")
    pf.add_argument("--variant", default="base", choices=sorted(VARIANTS))
    pf.add_argument("--all", action="store_true", help="base + 策略声明的全部需要回测的压力方案，并写组合回测清单")
    pf.add_argument("--timerange", default="20220101-")
    pf.add_argument("--idea", default="", help="想法 id（必填）：每次运行记入想法登记簿；判定只采用同一想法的运行")
    pf.add_argument("--trials", default="base,stop_floor",
                    help="--all 时一起跑的候选方案（非压力方案，含 base，至少 2 个实际不同）：上线判定的 PBO 与尝试次数来自它们（v2.10）")
    vd = sub.add_parser("verdict", help="上线判定：组合回测 + 策略声明的全部必需压力方案 + 登记的样本外段")
    vd.add_argument("--idea", required=True)
    vd.add_argument("--trial-sharpe-std", default="")
    sub.add_parser("fingerprint", help="打印当前版本指纹")
    g = sub.add_parser("g0")
    g.add_argument("--timerange", default="20250101-")
    a = ap.parse_args()

    if a.cmd == "plan":
        idea = check_idea(a.idea)
        y0, y1 = (int(x) for x in a.years.split("-"))
        req = strategy_requirements()
        variants = list(dict.fromkeys(a.variants.split(",") + [v for v in req["required_stress"] if v in RUN_VARIANTS]))
        for v in variants:
            if v not in VARIANTS:
                raise SystemExit(f"无效方案：{v}")
            problem = variant_problem(v, req)               # v2.9：计划阶段就拒绝与 base 相同的方案，不在矩阵里留下失败格子
            if problem:
                raise SystemExit(problem)
        trials = [v for v in variants if v not in RUN_VARIANTS]
        if len(trials) < 2:
            print(json.dumps({"warning": f"非压力方案只有 {trials}：上线判定的 PBO 至少需要 2 个，会判“证据不足”"},
                             ensure_ascii=False), file=sys.stderr)
        jobs = [f"{b}-{y}-{v}" for b in a.pairs.split(",") for y in range(y0, y1 + 1) for v in variants]
        for j in jobs:
            parse_job(j)
        ROOT.mkdir(exist_ok=True)
        fp = fingerprint()
        (ROOT / "jobs.json").write_text(json.dumps({"jobs": jobs, "fingerprint": fp["id"], "fingerprint_parts": fp, "idea": idea,
                                                    "required_stress": req["required_stress"], "strategy": req["strategy"],
                                                    "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                                   indent=1), "utf-8")
        print(f"{len(jobs)} 个任务 → research/jobs.json（版本指纹 {fp['id']}）")
        return 0
    if a.cmd == "run":
        RES.mkdir(parents=True, exist_ok=True)
        for old in (RES / f"{a.job}.json", RES / "trades" / f"{a.job}.csv"):   # 先删旧结果：失败时不会留下旧数据
            old.unlink(missing_ok=True)
        m = load_manifest()
        if a.job not in m["jobs"]:
            raise SystemExit(f"{a.job} 不在任务清单 research/jobs.json 里：先 research plan")
        out = backtest(a.job, RES / a.job, idea=check_idea(m.get("idea") or ""))
        tf = out.pop("_trades", None)
        if tf is not None:
            (RES / "trades").mkdir(parents=True, exist_ok=True)
            csv = RES / "trades" / f"{a.job}.csv"
            tf.to_csv(csv, index=False)
            out["csv_sha256"] = hashlib.sha256(csv.read_bytes()).hexdigest()   # v2.9：PBO 读矩阵时核对
        (RES / f"{a.job}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        print(json.dumps({k: out.get(k) for k in ("job", "status", "n_trades", "trades_hash", "fingerprint")},
                         ensure_ascii=False))
        return 0 if out["status"] == "ok" else 1
    if a.cmd == "verify":
        stored = json.loads((RES / f"{a.job}.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as d:
            fresh = backtest(a.job, Path(d))                 # 复核不记入想法登记簿（不是新的尝试）
            fresh.pop("_trades", None)
        cmp = compare_runs(stored, fresh)
        print(json.dumps({"job": a.job, **cmp}, ensure_ascii=False))
        return 0 if cmp["match"] else 1
    if a.cmd == "portfolio-verify":                          # v2.12：重跑一次组合回测（不写 latest、不记入想法登记簿）并核对
        vdir = PORTFOLIO / a.variant
        stored = json.loads((vdir / "latest.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as d:
            fresh: dict = {"variant": a.variant, "timerange": stored.get("timerange"), "run_id": "verify", "status": "running"}
            _portfolio_body(fresh, a.variant, stored.get("timerange"), None, None, Path(d))
        cmp = compare_runs(stored, fresh)
        print(json.dumps({"variant": a.variant, "run_id": stored.get("run_id"), **cmp}, ensure_ascii=False))
        return 0 if cmp["match"] else 1
    if a.cmd == "aggregate":
        m = load_manifest()
        cur = fingerprint()["id"]
        if m.get("fingerprint") != cur:                     # 清单本身属于旧版本：按当前版本重新判断
            m["fingerprint"] = cur
        rows, skipped = [], []
        for j in m["jobs"]:
            try:
                r = json.loads((RES / f"{j}.json").read_text("utf-8"))
            except (OSError, ValueError):
                skipped.append((j, "没有结果"))
                continue
            if r.get("status") != "ok":
                skipped.append((j, f"状态 {r.get('status')}"))
            elif m.get("fingerprint") and r.get("fingerprint") != m["fingerprint"]:
                skipped.append((j, "版本指纹不一致"))
            else:
                rows.append(r)
        lines = ["# 研究矩阵汇总（freqtrade，程序生成）", "", f"清单 {len(m['jobs'])} 个任务，纳入 {len(rows)} 个，"
                 f"排除 {len(skipped)} 个；版本指纹 {m.get('fingerprint')}。", "",
                 "| 任务 | 笔数 | 盈亏（USDT） | 胜率 | 逐笔哈希 |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {r['job']} | {r['n_trades']} | {r['profit_abs']} | {r['win_rate']} | {r['trades_hash']} |" for r in rows]
        if skipped:
            lines += ["", "排除的任务：", ""] + [f"- {j}：{why}" for j, why in skipped]
        base_n = sum(r["n_trades"] for r in rows if r["variant"] == "base")
        lines += ["", f"base 方案合计 {base_n} 笔；G1 要求 ≥ 100 笔（30–99 笔为“证据不足”）。",
                  "矩阵是单币 × 单年的诊断，合计笔数不等于一个账户的交易；上线证据看 portfolio。"]
        (ROOT / "matrix_report.md").write_text("\n".join(lines), "utf-8")
        print("\n".join(lines))
        return 0
    if a.cmd == "portfolio":
        idea = check_idea(a.idea)
        fp = fingerprint()
        req = strategy_requirements()
        trials = list(dict.fromkeys(["base"] + [v for v in a.trials.split(",") if v])) if a.all else []
        for v in trials:
            if v not in VARIANTS or v in RUN_VARIANTS:
                raise SystemExit(f"--trials 只能是非压力方案：{v}（可选 {sorted(set(VARIANTS) - RUN_VARIANTS)}）")
            problem = variant_problem(v, req)
            if problem:
                raise SystemExit(problem)
        if a.all and len(trials) < 2:
            print(json.dumps({"warning": "候选方案少于 2 个：上线判定的 PBO 会判“证据不足”"}, ensure_ascii=False), file=sys.stderr)
        variants = list(dict.fromkeys(["base"] + [v for v in req["required_stress"] if v in RUN_VARIANTS] + trials)) \
            if a.all else [a.variant]
        PORTFOLIO.mkdir(parents=True, exist_ok=True)
        if a.all:                                            # 组合回测清单：这一轮的实验标识、时间段、交易对、要求的方案、版本
            _, pairs = exchange_and_pairs()
            _write_atomic(PORTFOLIO / "manifest.json", {**req, "fingerprint": fp["id"], "timerange": a.timerange, "pairs": pairs,
                                                        "idea": idea, "trial_variants": trials,
                                                        "experiment_id": experiment_id(fp["id"], req, a.timerange, pairs, idea),
                                                        "variants": variants, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        rc = 0
        for v in variants:
            res = portfolio(v, a.timerange, fp, req, idea)
            rc |= 0 if res.get("status") == "ok" else 1
            print(json.dumps({k: res.get(k) for k in ("variant", "status", "run_id", "n_trades", "profit_abs",
                                                      "max_drawdown_closed", "max_concurrent", "data_gaps")},
                             ensure_ascii=False, default=str))
        return rc
    if a.cmd == "fingerprint":
        print(json.dumps(fingerprint(), ensure_ascii=False))
        return 0
    if a.cmd == "verdict":
        fp = fingerprint()["id"]                             # 清单或运行属于旧版本时，fwresearch verdict 直接拒绝（v2.8）
        args = [sys.executable, "-m", "fwresearch", "verdict", "--idea", a.idea, "--results", str(PORTFOLIO),
                "--matrix-results", str(RES), "--current-fingerprint", fp, "--ideas", str(IDEAS)] + \
            (["--trial-sharpe-std", a.trial_sharpe_std] if a.trial_sharpe_std else [])
        return subprocess.run(args).returncode
    if a.cmd == "g0":
        env = {**os.environ, "S03_EVENTS_REQUIRED": "0"}
        rc = 0
        for sub_cmd in (["lookahead-analysis"], ["recursive-analysis", "--startup-candle", "199", "400", "999"]):
            cmd = [freqtrade_bin(), *sub_cmd, *config_args(), *strategy_args(), "--timerange", a.timerange]
            print("$", " ".join(cmd), flush=True)
            rc |= subprocess.run(cmd, env=env).returncode
        return rc
    return 2


if __name__ == "__main__":
    sys.exit(main())
