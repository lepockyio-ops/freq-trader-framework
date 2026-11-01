"""Direction-specific execution failures, policies, risk and monitoring on real Freqtrade."""
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from test_framework_guarantees import F, NOW, EQUITY, FakeTrade, strat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))


PAIR = "BTC/USDT:USDT"


def short_tag(stop=101., cap=99.9):
    return F.encode_plan(stop, cap, NOW, "short_research", side="short")


def short_trade(stop=101., fill=100., amount=10.):
    t = FakeTrade(short_tag(stop), fill, amount)
    t.is_short, t.entry_side = True, "sell"
    return t


@pytest.mark.parametrize("stop,expected", [(101., 101.), (100., None), (99., None),
                                         (104., None), (float("inf"), None), (float("nan"), None)])
def test_short_guard_rejects_wrong_side_and_invalid(stop, expected):
    assert F.guarded_stop(100., stop, F.FW_DEFAULTS, True) == expected


def test_short_floor_and_liquidation_bound():
    fw = {**F.FW_DEFAULTS, "stop_min_mode": "floor"}
    assert F.guarded_stop(100., 100.5, fw, True) == pytest.approx(100.8)
    lev = F.isolated_leverage(.01, 100, .025, fw, True)
    assert (1 / lev - .025) / (1 + .025) >= fw["liq_to_stop_multiple"] * .01


def test_short_sizing_positive_and_rejects_tag_direction_replay():
    s = strat()
    lev = s.leverage(PAIR, NOW, 100., 10., 75., short_tag(), "short")
    stake = s.custom_stake_amount(PAIR, NOW, 100., 0, 0, 1e9, lev, short_tag(), "short")
    assert 0 < stake and stake * lev <= EQUITY
    assert s.custom_stake_amount(PAIR, NOW, 100., 0, 0, 1e9, lev, short_tag(), "long") == 0
    assert s.confirm_trade_entry(PAIR, "limit", stake * lev / 100., 100., "GTC", NOW, short_tag(), "short")
    assert not s.confirm_trade_entry(PAIR, "limit", 1., 100., "GTC", NOW, short_tag(), "long")


def test_short_minimum_sale_price_is_enforced_after_engine_clipping():
    s = strat()
    assert s.custom_entry_price(PAIR, None, NOW, 99., short_tag(), "short") == 99.9
    assert not s.confirm_trade_entry(PAIR, "limit", 1., 99.8, "GTC", NOW, short_tag(), "short")
    assert s.confirm_trade_entry(PAIR, "limit", 1., 100., "GTC", NOW, short_tag(), "short")


def test_short_fill_stop_risk_and_restart():
    s, t = strat(), short_trade()
    assert s._bind_stop(t) == 101.
    assert t.cd["fw_init_risk"] == 10.
    assert strat()._bind_stop(t) == 101.
    assert s._open_risk([t]) == 10.
    assert s.custom_stoploss(PAIR, t, NOW, 100., 0., False) == pytest.approx(.1)
    assert s.custom_exit(PAIR, t, NOW, 101.1, -.11) == "fw_stop_crossed"
    t.stop_loss = 101.
    assert not s._stop_crossed(t, 101., 101.1)
    assert s.custom_stoploss(PAIR, t, NOW, 101.1, -.11, False) is None


@pytest.mark.parametrize("cached", [99., 100., float("nan"), float("inf")])
def test_wrong_cached_short_stop_cannot_survive_restart(cached):
    s, t = strat(), short_trade()
    t.cd["stop_px"] = cached
    assert s._bind_stop(t) is None
    assert s.custom_exit(PAIR, t, NOW, 100., 0.).startswith("fw_invalid_stop:")


@pytest.mark.parametrize("policy", ["widen", "keep", "exit"])
def test_better_short_fill_policies(policy):
    s, t = strat({"better_fill_policy":policy}), short_trade(fill=100.5)
    stop = s._bind_stop(t)
    if policy == "exit":
        assert stop is None and t.cd["fw_invalid"] == "stop_too_close_after_fill"
    elif policy == "keep":
        assert stop == 101. and t.cd["stop_kept_below_min"]
    else:
        assert stop == pytest.approx(100.5 * 1.0082) and t.cd["stop_widened"]


def test_short_pending_risk_and_partial_profit_take():
    s, pending = strat(), short_trade(amount=0.)
    pending.nr_of_successful_entries = 0
    pending.amount_requested, pending.open_rate_requested = 10., 100.
    assert s._open_risk([pending]) == 10.
    t = short_trade()
    s.take_profit_legs = lambda trade: [(2., .5, "short_half")]
    args = (NOW, 98., 0., 0., 1e9, 98., 98., 0., 0.)
    assert s.adjust_trade_position(t, *args) == (-50., "short_half")
    assert F.target_reached(t, 98., 2.) and not F.target_reached(t, 98.01, 2.)
    t.amount = 5.
    assert s.adjust_trade_position(t, *args) is None


def test_legacy_plan_direction_and_unknown_side():
    old = "fw|s=99|c=100.1|t=1793000000|u=old|user"
    assert F.decode_plan(old)["side"] == "long"
    assert F.decode_plan(old)["user"] == "old|user"
    assert strat()._plan_stop(old, 100., "long") == 99.
    assert strat()._plan_stop(old, 100., "short") is None
    assert F.decode_plan(F.encode_plan(101., None, NOW, side="hedge")) is None


def test_policy_add_remove_and_blacklist():
    s = strat()
    assert all(s.pair_enabled(c + "/USDT:USDT") for c in ("BTC", "ETH", "SOL", "BNB", "HYPE"))
    s.config = {"exchange":{"pair_whitelist":["ADA/USDT:USDT"]}, "trading_policy":{"direction":"short"}}
    assert s.pair_enabled("ADA/USDT:USDT") and not s.pair_enabled(PAIR)
    assert s.side_enabled("short") and not s.side_enabled("long")
    s.config["exchange"]["pair_blacklist"] = ["ADA/.*"]
    assert not s.pair_enabled("ADA/USDT:USDT")
    s.config["exchange"]["pair_whitelist"] = []
    assert not s.pair_enabled(PAIR)
    s.config["trading_policy"]["direction"] = "shrot"
    with pytest.raises(ValueError): s.side_enabled("short")
    assert not s.confirm_trade_entry(PAIR, "limit", 1., 100., "GTC", NOW, short_tag(), "short")


def test_conflicting_signals_rejected_and_short_delay_carries_original_plan():
    s = strat({"research_entry_delay_bars":1})
    s.dp.runmode.value = "backtest"
    df = pd.DataFrame({"date":pd.date_range(NOW, periods=3, freq="15min"), "close":100.,
                       "enter_long":[1,0,0], "enter_short":[1,1,0], "fw_stop":[99.,101.,102.],
                       "fw_entry_cap":[100.,99.9,99.8], "enter_tag":["conflict","short",""]})
    out = s.advise_entry(df, {"pair":PAIR})
    assert out.enter_long.sum() == 0 and out.enter_short.tolist() == [0.,0.,1.]
    assert F.decode_plan(out.iloc[2].enter_tag)["stop"] == 101.
    assert F.decode_plan(out.iloc[2].enter_tag)["side"] == "short"


def test_short_review_r_and_monitor_include_sell_entry():
    from monitor.trade_risk import initial_risk, r_multiple
    row = {"is_short":True,"open_rate":100.,"stop_loss_abs":102.,"amount":5.,"profit_abs":20.}
    assert initial_risk(row) == (10., "stop_loss_abs")
    assert r_multiple(row) == (2., "stop_loss_abs")
    row["stop_loss_abs"] = 99.
    assert initial_risk(row)[0] is None
    from binance_monitor import positions_from_ft, partial_entries_from_ft
    row.update(is_open=True, pair=PAIR, stop_loss_abs=102., orders=[
        {"ft_order_side":"sell", "is_open":True, "filled":1., "order_timestamp":1000}])
    assert positions_from_ft([row])[0].is_short
    assert len(partial_entries_from_ft([row])) == 1
