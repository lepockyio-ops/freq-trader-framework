"""资产类别接口（v2.14）：美股永续等非 24/7 标的的时段、个股事件、只能更严格的参数、未声明 TradFi 合约。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data" / "strategies"))
sys.path.insert(0, str(ROOT / "tests"))
import framework_base as F  # noqa: E402
import fw_assets as A  # noqa: E402
from test_framework_guarantees import FakeTrade, strat, tag  # noqa: E402

UTC = timezone.utc
TSLA = "TSLA/USDT:USDT"
# 2026-11-23 周一（美东冬令时，常规时段 14:30–21:00 UTC）
MON_1500 = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
MON_OPEN5 = datetime(2026, 11, 23, 14, 35, tzinfo=UTC)
MON_CLOSE10 = datetime(2026, 11, 23, 20, 50, tzinfo=UTC)
SAT = datetime(2026, 11, 28, 15, 0, tzinfo=UTC)
WED_BEFORE_TG = datetime(2026, 11, 25, 20, 40, tzinfo=UTC)   # 感恩节前一天，收盘前 20 分钟（下一个交易日在 2 天后）
FRI_EARLY = datetime(2026, 11, 27, 17, 45, tzinfo=UTC)       # 感恩节次日 13:00 美东提前收盘 = 18:00 UTC


def cfg(**cls):
    base = {"pairs": [TSLA], "calendar": "nyse", "entry_after_open_min": 15, "entry_before_close_min": 15,
            "events_required": False}
    return {"fw_asset_classes": {"us_equity": {**base, **cls}}}


def confirm(s, now, pair=TSLA, side="long"):
    stop = 98.0 if side == "long" else 102.0
    return s.confirm_trade_entry(pair, "limit", 1, 100.0, "GTC", now, tag(stop), side)


@pytest.mark.parametrize("now,ok", [(MON_1500, True), (MON_OPEN5, False), (MON_CLOSE10, False), (SAT, False),
                                    (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), False),   # 感恩节休市
                                    (datetime(2026, 11, 23, 22, 0, tzinfo=UTC), False)])  # 收盘后
def test_session_gates_entries(now, ok):
    s = strat(config=cfg())
    assert confirm(s, now) is ok
    assert confirm(s, now, side="short") is ok


def test_crypto_pairs_are_not_gated():
    s = strat(config=cfg())
    assert confirm(s, SAT, pair="BTC/USDT:USDT")


def test_early_close_buffer():
    s = strat(config=cfg())
    assert not confirm(s, FRI_EARLY)                                       # 18:00 收盘，17:45 已在 15 分钟缓冲内
    assert confirm(s, FRI_EARLY - timedelta(minutes=30))


def test_extended_session():
    s = strat(config=cfg(calendar_params={"session": "extended"}))
    assert confirm(s, datetime(2026, 11, 23, 23, 0, tzinfo=UTC))           # 18:00 美东，盘后时段内


def test_flat_before_close_and_gap():
    t = FakeTrade(tag(98.0), 100.0, 1.0, pair=TSLA)
    t.open_date_utc = MON_1500 - timedelta(hours=1)
    t.cd["stop_px"] = 98.0
    s = strat(config=cfg(flat_before_close_min=10))
    assert s.custom_exit(TSLA, t, MON_1500, 100.0, 0.0) is None
    assert s.custom_exit(TSLA, t, MON_CLOSE10, 100.0, 0.0) == "fw_session_close"
    assert s.custom_exit(TSLA, t, SAT, 100.0, 0.0) == "fw_session_close"   # 收盘后还拿着（例如当时没运行）
    g = strat(config=cfg(flat_before_gap=True, flat_before_gap_min=30))
    assert g.custom_exit(TSLA, t, MON_CLOSE10, 100.0, 0.0) is None         # 周一收盘：次日就开盘
    assert g.custom_exit(TSLA, t, WED_BEFORE_TG, 100.0, 0.0) == "fw_session_gap"
    assert g.custom_exit(TSLA, t, SAT, 100.0, 0.0) == "fw_session_gap"
    assert g.custom_exit(TSLA, t, datetime(2026, 11, 24, 2, 0, tzinfo=UTC), 100.0, 0.0) is None   # 平日夜间不算长休市
    assert not g.confirm_trade_entry(TSLA, "limit", 1, 100.0, "GTC", WED_BEFORE_TG, tag(98.0), "long")


def _events(tmp_path, events, generated=None):
    f = tmp_path / "eq_events.json"
    f.write_text(json.dumps({"generated_at": (generated or datetime.now(UTC)).isoformat(), "events": events}), "utf-8")
    return str(f)


def test_equity_events_block_and_flatten(tmp_path):
    ev = _events(tmp_path, [{"symbol": "TSLA", "time_utc": "2026-11-23T21:05:00Z", "type": "earnings"}],
                 generated=MON_1500)
    s = strat(config=cfg(events_file=ev, events_required=True, event_before_min=120, event_after_min=60))
    assert confirm(s, MON_1500 - timedelta(minutes=0))                     # 15:00，事件在 6 小时后
    assert not confirm(s, datetime(2026, 11, 23, 19, 30, tzinfo=UTC))      # 事件前 95 分钟
    t = FakeTrade(tag(98.0), 100.0, 1.0, pair=TSLA)
    t.cd["stop_px"] = 98.0
    assert s.custom_exit(TSLA, t, datetime(2026, 11, 23, 19, 30, tzinfo=UTC), 100.0, 0.0) == "fw_equity_event:earnings"
    assert s.custom_exit(TSLA, t, MON_1500, 100.0, 0.0) is None
    assert confirm(s, MON_1500, pair="BTC/USDT:USDT")                       # 只影响同一个代码


@pytest.mark.parametrize("problem", ["missing", "bad", "stale", "unset"])
def test_required_events_fail_closed_live_but_not_in_backtest(tmp_path, problem):
    f = {"missing": str(tmp_path / "nope.json"), "unset": "",
         "bad": str(tmp_path / "bad.json"), "stale": _events(tmp_path, [], generated=MON_1500 - timedelta(days=10))}[problem]
    if problem == "bad":
        Path(f).write_text("{not json", "utf-8")
    s = strat(config=cfg(events_file=f, events_required=True))
    assert not confirm(s, MON_1500)
    s.dp.runmode = SimpleNamespace(value="backtest")
    assert confirm(s, MON_1500)
    s2 = strat(config=cfg(events_file=f, events_required=False))
    assert confirm(s2, MON_1500)


def test_class_params_are_applied_and_only_stricter():
    s = strat(config=cfg(params={"risk_per_trade": 0.0025, "max_position_leverage": 5, "stop_min_pct": 1.0}))
    fw = s.fw_for(TSLA)
    assert fw["risk_per_trade"] == 0.0025 and fw["max_position_leverage"] == 5 and fw["stop_min_pct"] == 1.0
    assert s.fw_for("BTC/USDT:USDT")["risk_per_trade"] == F.FW_DEFAULTS["risk_per_trade"]
    lev = s.leverage(TSLA, MON_1500, 100.0, 10, 75, tag(98.0), "long")
    assert lev <= 5
    st_eq = s.custom_stake_amount(TSLA, MON_1500, 100.0, 0, 0, 1e9, lev, tag(98.0), "long")
    st_bt = s.custom_stake_amount("BTC/USDT:USDT", MON_1500, 100.0, 0, 0, 1e9, lev, tag(98.0), "long")
    assert abs(st_eq / st_bt - 0.5) < 1e-9                                 # 单笔风险减半
    assert s.custom_stake_amount(TSLA, MON_1500, 100.0, 0, 0, 1e9, lev, tag(99.2), "long") == 0   # 0.8% 低于类别下限 1.0%
    loose = strat(config=cfg(params={"risk_per_trade": 0.01}))
    with pytest.raises(ValueError, match="更宽松"):
        loose.bot_start()
    assert loose.custom_stake_amount(TSLA, MON_1500, 100.0, 0, 0, 1e9, 1, tag(98.0), "long") == 0
    assert not confirm(loose, MON_1500)
    with pytest.raises(ValueError, match="不能按类别覆盖"):
        strat(config=cfg(params={"max_open_risk": 0.001})).bot_start()


def test_bind_stop_uses_class_budget():
    s = strat(config=cfg(params={"risk_per_trade": 0.0025}))
    t = FakeTrade(tag(98.0), 100.0, 20.0, pair=TSLA)                       # 风险 40 > 10000 × 0.0025 × 1.05
    assert s._bind_stop(t) is None and t.cd["fw_invalid"] == "risk_over_budget"
    b = FakeTrade(tag(98.0), 100.0, 20.0, pair="BTC/USDT:USDT")            # 同样的仓位在加密货币类别里合规
    assert s._bind_stop(b) == 98.0


def test_class_max_hold():
    s = strat(config=cfg(params={"max_hold_days": 2}, calendar="24x7"))
    t = FakeTrade(tag(98.0), 100.0, 1.0, pair=TSLA)
    t.cd["stop_px"] = 98.0
    t.open_date_utc = MON_1500 - timedelta(days=3)
    assert s.custom_exit(TSLA, t, MON_1500, 100.0, 0.0) == "max_hold"


def test_undeclared_tradfi_market_rejected():
    s = strat(config={})
    s.dp.market = lambda p: {"info": {"contractType": "TRADIFI_PERPETUAL"}} if p.startswith("NVDA") else {"info": {"contractType": "PERPETUAL"}}
    assert not confirm(s, MON_1500, pair="NVDA/USDT:USDT")
    assert confirm(s, MON_1500, pair="BTC/USDT:USDT")
    d = strat(config={"fw_asset_classes": {"us_equity": {"pairs": ["NVDA/USDT:USDT"], "calendar": "nyse"}}})
    d.dp.market = s.dp.market
    assert not confirm(d, MON_1500, pair="NVDA/USDT:USDT")                 # 默认 events_required：没有个股事件文件也拒绝
    d.config["fw_asset_classes"]["us_equity"]["events_required"] = False
    assert confirm(d, MON_1500, pair="NVDA/USDT:USDT")
    c = strat(config={"fw_tradfi_markers": {"underlyingType": ["EQUITY"]}})
    c.dp.market = lambda p: {"info": {"underlyingType": "EQUITY"}}
    assert not confirm(c, MON_1500, pair="X/USDT:USDT")


@pytest.mark.parametrize("bad,msg", [
    ({"calendar": "lse"}, "未知的交易日历"),
    ({"calendar_params": {"session": "night"}}, "regular 或 extended"),
    ({"entry_after_open_min": -1}, "不能为负"),
    ({"foo": 1}, "未知字段"),
    ({"params": {"stop_min_pct": 2.0, "stop_max_pct": 1.5}}, "stop_min_pct"),
])
def test_bad_class_config_raises(bad, msg):
    with pytest.raises(ValueError, match=msg):
        strat(config=cfg(**bad)).bot_start()


def test_pair_in_two_classes_raises():
    c = {"fw_asset_classes": {"a": {"pairs": [TSLA]}, "b": {"pairs": [TSLA]}}}
    with pytest.raises(ValueError, match="同时属于"):
        strat(config=c).bot_start()


def test_shipped_configs_have_empty_us_equity_class():
    for name in ("config.json", "config-test-okx.json"):
        c = json.loads((ROOT / "user_data" / name).read_text("utf-8"))
        classes = A.parse_classes(c)
        assert classes["us_equity"]["pairs"] == [] and classes["us_equity"]["calendar"] == "nyse"


def test_doctor_asset_class_checks(tmp_path):
    sys.path.insert(0, str(ROOT))
    from s03ft import cli

    def run(cfg_dict):
        out = []
        cli.asset_class_checks(lambda n, ok, d="": out.append((n, bool(ok), d)), Path("c.json"), cfg_dict,
                               list((cfg_dict.get("exchange") or {}).get("pair_whitelist") or []))
        return out

    shipped = json.loads((ROOT / "user_data" / "config.json").read_text("utf-8"))
    assert all(ok for _, ok, _ in run(shipped))
    ev = _events(tmp_path, [{"symbol": "TSLA", "time_utc": "2027-01-28T21:00:00Z", "type": "earnings"}])
    good = {"exchange": {"pair_whitelist": [TSLA]}, **cfg(events_file=ev, events_required=True)}
    assert all(ok for _, ok, _ in run(good))
    bad = {"exchange": {"pair_whitelist": []}, **cfg(events_required=True, params={"risk_per_trade": 0.02})}
    assert not run(bad)[0][1]
    missing = {"exchange": {"pair_whitelist": []}, **cfg(events_required=True)}
    res = {n: ok for n, ok, _ in run(missing)}
    assert [ok for n, ok in res.items() if "白名单" in n] == [False]
    assert [ok for n, ok in res.items() if "个股事件" in n] == [False]
