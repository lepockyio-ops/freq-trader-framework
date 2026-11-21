"""模拟盘与回测的交易所端口（v2.15）：Binance / OKX / Hyperliquid 配置、TradFi 识别、fw_sim_only、真实回测。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import fw_assets as A  # noqa: E402
from fw_sessions import NyseCalendar  # noqa: E402
from s03ft import sim  # noqa: E402
from test_framework_guarantees import strat, tag  # noqa: E402

MON = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
STOCK = ("TSLA", "NVDA", "AAPL", "SPY", "QQQ", "SP500", "XYZ100")


def load(e):
    return json.loads(sim.config_path(ROOT, e).read_text("utf-8"))


@pytest.mark.parametrize("e", sim.EXCHANGES)
def test_sim_configs_are_simulation_only_and_keyless(e):
    c = load(e)
    assert c["dry_run"] is True and c["fw_sim_only"] is True and c["initial_state"] == "running"
    assert not any(c["exchange"].get(k) for k in sim.SECRET_FIELDS)
    assert c["trading_mode"] == "futures" and c["margin_mode"] == "isolated"
    cls = A.parse_classes(c)["us_equity"]
    wl = c["exchange"]["pair_whitelist"]
    assert cls["pairs"] and set(cls["pairs"]) <= set(wl) and cls["calendar"] == "nyse"
    crypto = [p for p in wl if p not in cls["pairs"]]
    assert [p.split("/")[0] for p in crypto] == ["BTC", "ETH", "SOL", "BNB", "HYPE"]
    out = []
    sim.sim_checks(lambda n, ok, d="": out.append(ok), sim.config_path(ROOT, e), ROOT)
    assert all(out)


@pytest.mark.parametrize("e", sim.EXCHANGES)
def test_sim_configs_pass_freqtrade_schema(e):
    from freqtrade.configuration.config_validation import validate_config_schema

    validate_config_schema(load(e))


def test_exchange_specific_settings():
    hl = load("hyperliquid")
    assert hl["stake_currency"] == "USDC" and hl["exchange"]["hip3_dexes"] == ["xyz"]
    assert hl["order_types"]["stoploss"] == "limit" and "stoploss_price_type" not in hl["order_types"]   # freqtrade：Hyperliquid 只支持限价止损
    for e in ("binance", "okx"):
        assert load(e)["stake_currency"] == "USDT" and load(e)["order_types"]["stoploss_price_type"] == "mark"


def test_binance_stock_pairs_exist_in_freqtrade_tiers():
    """Binance API 在本环境返回 451；用 freqtrade 2026.10 自带的 Binance 杠杆档位表核对交易对存在。"""
    import freqtrade

    tiers = json.loads((Path(freqtrade.__file__).parent / "exchange" / "binance_leverage_tiers.json").read_text("utf-8"))
    for p in A.parse_classes(load("binance"))["us_equity"]["pairs"]:
        assert p in tiers, p


def test_fw_sim_only_refuses_live():
    s = strat(config={"fw_sim_only": True, "dry_run": False})
    with pytest.raises(ValueError, match="fw_sim_only"):
        s.bot_start()
    s = strat(config={"fw_sim_only": True, "dry_run": True})
    s.dp.runmode.value = "live"
    with pytest.raises(ValueError, match="fw_sim_only"):
        s.bot_start()
    strat(config={"fw_sim_only": True, "dry_run": True}).bot_start()


@pytest.mark.parametrize("info,tradfi", [
    ({"instCategory": "1"}, False),                       # OKX 加密货币（已核验）
    ({"instCategory": "3"}, True),                        # OKX 股票
    ({"instCategory": "4"}, True),                        # OKX 商品
    ({"hip3": True, "dex": "xyz"}, True),                 # Hyperliquid HIP-3
    ({"hip3": False}, False),
    ({"contractType": "TRADIFI_PERPETUAL"}, True),        # Binance（来源方观点）
    ({"contractType": "PERPETUAL"}, False),
    ({}, False),
])
def test_tradfi_markers(info, tradfi):
    assert A.is_tradfi_market({"info": info}) is tradfi


def test_undeclared_okx_stock_and_hip3_rejected_declared_allowed():
    s = strat(config={})
    s.dp.market = lambda p: {"info": {"instCategory": "3"}} if p.startswith("AMZN") else {"info": {"hip3": True}}
    assert not s.confirm_trade_entry("AMZN/USDT:USDT", "limit", 1, 100.0, "GTC", MON, tag(98.0), "long")
    assert not s.confirm_trade_entry("XYZ-AMZN/USDC:USDC", "limit", 1, 100.0, "GTC", MON, tag(98.0), "long")
    d = strat(config={"fw_asset_classes": {"us_equity": {"pairs": ["XYZ-AMZN/USDC:USDC"], "calendar": "nyse",
                                                         "events_required": False}}})
    d.dp.market = s.dp.market
    assert d.confirm_trade_entry("XYZ-AMZN/USDC:USDC", "limit", 1, 100.0, "GTC", MON, tag(98.0), "long")


def test_hip3_symbol_matches_equity_events(tmp_path):
    f = tmp_path / "ev.json"
    f.write_text(json.dumps({"generated_at": MON.isoformat(), "events": [
        {"symbol": "TSLA", "time_utc": "2026-11-23T21:05:00Z", "type": "earnings"}]}), "utf-8")
    assert A.symbol_aliases("XYZ-TSLA/USDC:USDC") == {"XYZ-TSLA", "TSLA"}
    s = strat(config={"fw_asset_classes": {"us_equity": {"pairs": ["XYZ-TSLA/USDC:USDC"], "calendar": "nyse",
                                                         "events_file": str(f), "event_before_min": 120}}})
    assert not s.confirm_trade_entry("XYZ-TSLA/USDC:USDC", "limit", 1, 100.0, "GTC",
                                     datetime(2026, 11, 23, 19, 30, tzinfo=timezone.utc), tag(98.0), "long")


def _has_data(e, pairs):
    d = ROOT / "user_data" / "data" / e / "futures"
    return all((d / f"{p.replace('/', '_').replace(':', '_')}-15m-futures.feather").exists() for p in pairs)


@pytest.mark.parametrize("e,timerange", [("okx", "20261001-20261120"), ("hyperliquid", "20261005-20261120")])
def test_sim_backtest_real_freqtrade(tmp_path, e, timerange):
    """真实 freqtrade 回测：加密货币与美股永续都能跑；美股永续的入场都在 NYSE 时段缓冲内。需要本地数据（s03ft sim data）。"""
    wl = load(e)["exchange"]["pair_whitelist"]
    if shutil.which("freqtrade") is None or not _has_data(e, wl):
        pytest.skip(f"需要 freqtrade 与本地 {e} 数据：s03ft sim data --exchange {e}")
    cmd = ["freqtrade", "backtesting", "-c", str(sim.config_path(ROOT, e)), "--strategy", "SmaCrossExample",
           "--strategy-path", "examples/strategies", "--timerange", timerange, "--export", "trades",
           "--backtest-directory", str(tmp_path), "--cache", "none"]
    if os.environ.get("S03_FT_EXTRA_CONFIG"):
        cmd += ["-c", os.environ["S03_FT_EXTRA_CONFIG"]]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env={**os.environ, "S03_EVENTS_REQUIRED": "0"})
    assert r.returncode == 0, r.stderr[-2000:]
    from freqtrade.data.btanalysis import load_backtest_data

    t = load_backtest_data(tmp_path)
    stock = t[t["pair"].str.split("/").str[0].str.split("-").str[-1].isin(STOCK)]
    assert len(stock) > 0 and len(t) > len(stock)
    cal = NyseCalendar()
    for d in stock["open_date"]:
        st = cal.status(pd.Timestamp(d).to_pydatetime())
        assert st.is_open and st.minutes_since_open >= 15 and st.minutes_to_close > 15, d
    assert t["enter_tag"].str.startswith("fw|").all()
