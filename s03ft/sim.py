"""模拟盘与回测的交易所端口（v2.15）：Binance、OKX、Hyperliquid，加密货币与美股永续。

只用交易所的公开行情接口，不需要也不读取任何 API 密钥；配置都标了 fw_sim_only（框架在实盘模式下拒绝启动）。

  s03ft sim list                                     # 三个交易所的配置、交易对、检查结果
  s03ft sim data --exchange okx [--timerange 20260901-] [--timeframes 15m 1d]
  s03ft sim backtest --exchange okx --strategy MyStrategy [--timerange 20261001-20261120] [--strategy-path DIR]
  s03ft sim dryrun --exchange hyperliquid            # 前台运行 freqtrade 模拟盘（Ctrl+C 停止）

Hyperliquid 只提供最近 5000 根 K 线，freqtrade 的 download-data 因此拒绝下载（“Historic data not available”）。
这里改用 freqtrade 自己的逐对下载函数取最近一段并与本地已有数据合并：定期运行（例如每周）历史会越积越长。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

EXCHANGES = ("binance", "okx", "hyperliquid")
SECRET_FIELDS = ("key", "secret", "password", "uid", "walletAddress", "privateKey")


def config_path(home: Path, exchange: str) -> Path:
    if exchange not in EXCHANGES:
        raise ValueError(f"交易所必须是 {EXCHANGES} 之一")
    return home / "user_data" / f"config-sim-{exchange}.json"


def sim_checks(add, cf: Path, home: Path) -> None:
    """模拟配置：是模拟盘、标了 fw_sim_only、没有填任何密钥字段、资产类别有效且交易对在白名单里。"""
    try:
        cfg = json.loads(cf.read_text("utf-8"))
    except (OSError, ValueError) as e:
        add(f"{cf.name} 可读", False, str(e))
        return
    ex = cfg.get("exchange") or {}
    add(f"{cf.name} 是模拟盘（dry_run = true、fw_sim_only = true）", cfg.get("dry_run") is True and cfg.get("fw_sim_only") is True,
        f"dry_run = {cfg.get('dry_run')}，fw_sim_only = {cfg.get('fw_sim_only')}")
    filled = [k for k in SECRET_FIELDS if ex.get(k)]
    add(f"{cf.name} 没有填写任何密钥字段", not filled, f"已填写：{filled}（模拟盘与回测不需要，请删除）" if filled else "")
    sys.path.insert(0, str(home / "user_data" / "strategies"))
    import fw_assets as A
    from framework_base import FW_DEFAULTS

    wl = list(ex.get("pair_whitelist") or [])
    try:
        classes = A.parse_classes(cfg)
        for name, c in classes.items():
            A.merge_params(FW_DEFAULTS, c["params"], name)
        miss = [p for c in classes.values() for p in c["pairs"] if p not in wl]
        err = f"类别交易对不在白名单：{miss}" if miss else ""
    except ValueError as e:
        classes, err = {}, str(e)
    add(f"{cf.name} 的资产类别有效", not err,
        err or "；".join(f"{n}: {len(c['pairs'])} 个" for n, c in classes.items()))


def _extra(a) -> list[str]:
    out = []
    for c in (a.config or []):
        out += ["-c", c]
    if os.environ.get("S03_FT_EXTRA_CONFIG"):                 # 本机专属设置（如代理证书），不进仓库
        out += ["-c", os.environ["S03_FT_EXTRA_CONFIG"]]
    return out


def snapshot(cfg_files: list[str], timeframes: list[str], timerange: str, datadir: Path) -> list[str]:
    """Hyperliquid：用 freqtrade 的逐对下载取最近一段（每个周期最多约 5000 根）并合并进本地数据。返回取不到的交易对。"""
    from freqtrade.configuration import Configuration
    from freqtrade.configuration.timerange import TimeRange
    from freqtrade.data.history.history_utils import refresh_backtest_ohlcv_data
    from freqtrade.enums import RunMode
    from freqtrade.resolvers import ExchangeResolver

    cfg = Configuration.from_files(cfg_files)
    cfg["runmode"] = RunMode.UTIL_EXCHANGE
    cfg["datadir"] = datadir
    ex = ExchangeResolver.load_exchange(cfg, validate=False)
    pairs = list(cfg["exchange"]["pair_whitelist"])
    return refresh_backtest_ohlcv_data(ex, pairs=pairs, timeframes=timeframes, datadir=datadir, trading_mode="futures",
                                       timerange=TimeRange.parse_timerange(timerange) if timerange else None,
                                       data_format="feather")


def cmd_sim(a, home: Path, emit) -> int:
    if a.sub == "list":
        rows, ok_all = [], True
        for e in EXCHANGES:
            cf = config_path(home, e)
            checks = []
            sim_checks(lambda n, ok, d="": checks.append({"check": n, "ok": bool(ok), "detail": d}), cf, home)
            cfg = json.loads(cf.read_text("utf-8")) if cf.exists() else {}
            ok_all &= all(c["ok"] for c in checks)
            rows.append({"exchange": e, "config": str(cf.relative_to(home)), "stake": cfg.get("stake_currency"),
                         "pairs": (cfg.get("exchange") or {}).get("pair_whitelist"), "checks": checks})
        text = "\n".join(f"{r['exchange']}: {r['config']}（{r['stake']}）{', '.join(r['pairs'] or [])}\n" +
                         "\n".join(f"  {'✓' if c['ok'] else '✗'} {c['check']}  {c['detail']}" for c in r["checks"]) for r in rows)
        return emit({"ok": ok_all, "exchanges": rows}, a.json, text)
    cf = config_path(home, a.exchange)
    cfg = json.loads(cf.read_text("utf-8"))
    if cfg.get("dry_run") is not True or cfg.get("fw_sim_only") is not True:
        print(f"{cf.name} 不是模拟配置（dry_run / fw_sim_only），拒绝运行", file=sys.stderr)
        return 2
    base = ["-c", str(cf)] + _extra(a)
    if a.sub == "data":
        tfs = a.timeframes or ["15m", "1d"]
        if a.exchange == "hyperliquid":
            files = [str(cf)] + [x for x in _extra(a) if x != "-c"]
            missing = snapshot(files, tfs, a.timerange, home / "user_data" / "data" / "hyperliquid")
            print("取不到：" + ", ".join(missing) if missing else "完成（Hyperliquid 每个周期只有最近约 5000 根，定期运行以累积历史）")
            return 1 if missing else 0
        cmd = ["freqtrade", "download-data", *base, "--timeframes", *tfs, "--trading-mode", "futures"]
        if a.timerange:
            cmd += ["--timerange", a.timerange]
        return subprocess.run(cmd, cwd=home).returncode
    if a.sub == "backtest":
        cmd = ["freqtrade", "backtesting", *base, "--strategy", a.strategy, "--cache", "none", "--export", "trades"]
        if a.strategy_path:
            cmd += ["--strategy-path", a.strategy_path]
        if a.timerange:
            cmd += ["--timerange", a.timerange]
        env = {**os.environ, "S03_EVENTS_REQUIRED": os.environ.get("S03_EVENTS_REQUIRED", "0")}
        return subprocess.run(cmd, cwd=home, env=env).returncode
    if a.sub == "dryrun":
        cmd = ["freqtrade", "trade", *base]
        if a.strategy:
            cmd += ["--strategy", a.strategy]
        if a.strategy_path:
            cmd += ["--strategy-path", a.strategy_path]
        return subprocess.run(cmd, cwd=home).returncode
    return 2


def add_parser(sub, fn) -> None:
    p = sub.add_parser("sim", help="模拟盘与回测的交易所端口：binance / okx / hyperliquid（只用公开行情，不需要密钥）")
    p.add_argument("sub", choices=["list", "data", "backtest", "dryrun"])
    p.add_argument("--exchange", choices=EXCHANGES)
    p.add_argument("--timerange")
    p.add_argument("--timeframes", nargs="+")
    p.add_argument("--strategy")
    p.add_argument("--strategy-path")
    p.add_argument("-c", "--config", action="append", help="附加配置（例如本机代理证书），可重复")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=fn)
