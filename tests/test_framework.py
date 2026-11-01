"""框架测试：空白模板不入场；示例策略的信号带合规止损；freqtrade 实际回测（有本地数据时）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
sys.path.insert(0, str(ROOT / "examples" / "strategies"))
import framework_base as F  # noqa: E402
from MyStrategy import MyStrategy  # noqa: E402


def _df(n=300, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.004, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"date": pd.date_range("2026-10-01", periods=n, freq="15min", tz="UTC"), "open": o,
                         "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.996, "close": c,
                         "volume": 1.0})


def test_template_is_blank():
    s = MyStrategy.__new__(MyStrategy)
    assert s.is_template is True and isinstance(s, F.FrameworkStrategy)
    df = s.populate_entry_trend(s.populate_indicators(_df(), {}), {})
    assert "enter_long" not in df or df["enter_long"].fillna(0).sum() == 0
    assert s.confirm_trade_entry("BTC/USDT:USDT", "limit", 1, 100, "GTC", pd.Timestamp.now(tz="UTC"), "", "long") is False


def test_guarded_stop_cases():
    fw = F.FW_DEFAULTS
    assert F.guarded_stop(100, 99.5, {**fw, "stop_min_mode": "skip"}) is None
    assert abs(F.guarded_stop(100, 99.5, {**fw, "stop_min_mode": "floor"}) - 99.2) < 1e-9
    assert F.guarded_stop(100, 96, fw) is None and F.guarded_stop(100, 98, fw) == 98
    assert F.guarded_stop(100, float("nan"), fw) is None and F.guarded_stop(100, None, fw) is None


def test_example_signals_carry_stop():
    from SmaCrossExample import SmaCrossExample

    s = SmaCrossExample.__new__(SmaCrossExample)
    df = s.populate_indicators(_df(), {})
    df["trend_ok_1d"] = 1
    df["trend_down_1d"] = 1
    df = s.populate_entry_trend(df, {})
    sig = df[df["enter_long"] == 1]
    assert len(sig) > 0 and (sig["fw_stop"] < sig["close"]).all() and sig["fw_entry_cap"].notna().all()
    sht = df[(df["enter_short"] == 1) & df["fw_stop"].notna()]              # v2.13：空单止损在上方、限价下限在下方（预热期 NaN 由框架拒绝）
    assert len(sht) > 0 and (sht["fw_stop"] > sht["close"]).all() and (sht["fw_entry_cap"] < sht["close"]).all()
    assert s.take_profit_legs(None) == [(2.0, 0.5, "tp_2R")]


DATA = ROOT / "user_data" / "data" / "okx" / "futures"


@pytest.mark.skipif(shutil.which("freqtrade") is None or not DATA.exists(), reason="需要 freqtrade 与本地 OKX 测试数据")
@pytest.mark.parametrize("strategy,path,expect_trades,direction", [("MyStrategy", None, False, "both"),
                                                                   ("SmaCrossExample", "examples/strategies", True, "both"),
                                                                   ("SmaCrossExample", "examples/strategies", True, "long"),
                                                                   ("SmaCrossExample", "examples/strategies", True, "short")])
def test_freqtrade_backtest_smoke(tmp_path, strategy, path, expect_trades, direction):
    cfg = json.loads((ROOT / "user_data" / "config-test-okx.json").read_text("utf-8"))
    cfg["fw_trade_direction"] = direction                        # v2.13：方向由配置决定
    (tmp_path / "cfg.json").write_text(json.dumps(cfg), "utf-8")
    cmd = ["freqtrade", "backtesting", "-c", str(tmp_path / "cfg.json"), "--strategy", strategy,
           "--timerange", "20261005-20261026", "-p", "SOL/USDT:USDT", "BTC/USDT:USDT", "--export", "trades",
           "--backtest-directory", str(tmp_path), "--cache", "none"]       # 回测缓存不含框架基类：必须关掉
    if path:
        cmd += ["--strategy-path", path]
    if os.environ.get("S03_FT_EXTRA_CONFIG"):                       # 本机专属设置（如代理证书）放在仓库外
        cmd += ["-c", os.environ["S03_FT_EXTRA_CONFIG"]]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env={**os.environ, "S03_EVENTS_REQUIRED": "0"})
    assert r.returncode == 0, r.stderr[-2000:]
    from freqtrade.data.btanalysis import load_backtest_data

    t = load_backtest_data(tmp_path)
    if not expect_trades:
        assert len(t) == 0
        return
    assert len(t) > 0
    init = t.groupby("trade_id").first() if "trade_id" in t else t
    short = init["is_short"].astype(bool)
    if direction == "long":
        assert not short.any()
    elif direction == "short":
        assert short.all()
    else:
        assert short.any() and (~short).any(), "测试区间里多空都应出现"
    # 止损随订单绑定，按成交价检查；距离必须在 0.8%–3%（成交价更优时放宽到 0.8%）；多单止损在下方，空单在上方
    sgn = short.map({True: 1.0, False: -1.0})
    stop_pct = (init["stop_loss_abs"] - init["open_rate"]) / init["open_rate"] * 100 * sgn
    assert ((stop_pct >= 0.8 - 1e-6) & (stop_pct <= 3.0 + 1e-6)).all(), stop_pct.tolist()
    assert ((init["initial_stop_loss_abs"] - init["stop_loss_abs"]) * sgn > 0).all()   # initial 是兜底的 99%，比策略止损更远
    rr = init[init["exit_reason"] == "stop_loss"]                           # 止损离场约 −1R（含手续费、取整）
    rr = rr[[not any(not o.get("ft_is_entry") and o.get("ft_order_side") != "stoploss" and o.get("ft_order_tag") == "tp_2R"
                     for o in os_) for os_ in rr["orders"]]]                 # 分批止盈过的交易剩余仓位更小，另算
    risk = (rr["stop_loss_abs"] - rr["open_rate"]).abs() * rr["amount"]
    assert rr.empty or ((rr["profit_abs"] / risk) < -0.8).all(), (rr["profit_abs"] / risk).tolist()
    assert init["enter_tag"].str.startswith("fw|").all()                   # 计划随订单保存
    # 周期以策略为准（15m）：入场发生在 15 分钟 K 线的开盘时刻，说明配置没有把它改成 1m
    assert (pd.to_datetime(init["open_date"]).dt.minute % 15 == 0).all()
