import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor"))
from review_metrics import compute, from_freqtrade  # noqa: E402


def test_r_uses_entry_fill_amount_after_partial_exits():
    t = {"pair": "SOL/USDT:USDT", "is_open": False, "open_rate": 100.0, "stop_loss_abs": 98.0, "amount": 5.0,
         "profit_abs": 60.0, "exit_reason": "ema9_break", "close_date": "2026-10-03",
         "orders": [{"ft_is_entry": True, "filled": 10.0}, {"ft_is_entry": False, "filled": 5.0}]}
    rows = from_freqtrade([{**t, "trade_id": 1}, {**t, "is_open": True}], {1: 20.0})   # 记录的初始风险：10 × 2
    assert len(rows) == 1 and rows[0]["r"] == 3.0          # 60 / (10 × 2)
    assert compute(rows)["by_coin"]["SOL"]["expectancy_r"] == 3.0
    est = from_freqtrade([t], None, {"stop_min_pct": 0.8, "stop_max_pct": 3.0})        # 没有记录：只给估算值
    assert est[0]["r"] is None and est[0]["r_est"] == 3.0 and est[0]["risk_source"] == "no_record"


def test_total_streak_uses_account_close_order():
    """审查 P2：合计先按币分组再拼接，真实 2 连亏被报告成 1。"""
    trs = [{"coin": "BTC", "close_time": "2026-01-01", "r": -1, "pnl": -1, "exit_reason": "sl"},
           {"coin": "ETH", "close_time": "2026-01-02", "r": -1, "pnl": -1, "exit_reason": "sl"},
           {"coin": "BTC", "close_time": "2026-01-03", "r": 1, "pnl": 1, "exit_reason": "tp"},
           {"coin": "ETH", "close_time": "2026-01-04", "r": 1, "pnl": 1, "exit_reason": "tp"}]
    m = compute(trs)
    assert m["total"]["max_consecutive_losses"] == 2 and m["by_coin"]["BTC"]["max_consecutive_losses"] == 1


def test_strategy_tag_recovered_from_framework_enter_tag():
    from review_metrics import strategy_tag

    assert strategy_tag("fw|s=99|c=|t=1|u=breakout") == "breakout"
    assert strategy_tag("fw|s=99|c=|t=1") == "(无)" and strategy_tag("manual") == "manual"
    trs = from_freqtrade([{"is_open": False, "pair": "BTC/USDT:USDT", "profit_abs": 10, "open_rate": 100, "stop_loss_abs": 99,
                           "amount": 10, "orders": [], "close_date": "2026-01-01", "exit_reason": "roi",
                           "enter_tag": "fw|s=99|c=|t=1|u=breakout"}])
    assert compute(trs)["by_tag"]["breakout"]["n"] == 1


# ------------------------------------------------------------------ v2.10：初始风险与 R 的统一口径（外部审查 v2.9 P1-1）
import pytest  # noqa: E402
from trade_risk import initial_risk, r_multiple  # noqa: E402

_T = {"is_open": False, "pair": "SOL/USDT:USDT", "open_rate": 100.0, "profit_abs": -2.0, "amount": 1.0,
      "enter_tag": "fw|s=99|c=|t=1", "exit_reason": "fw_stop_crossed", "close_date": "2026-10-03",
      "orders": [{"ft_is_entry": True, "amount": 1.0, "filled": 1.0}]}


@pytest.mark.parametrize("trade,expect_r,src", [
    ({**_T, "stop_loss_abs": 90.1}, -2.0, "plan"),                                  # 兜底止损：v2.9 算成约 −0.2R
    ({**_T, "stop_loss_abs": 99.0}, -2.0, "stop_loss_abs"),                        # 正常
    ({**_T, "stop_loss_abs": 98.996}, -2.0 / 1.004, "stop_loss_abs"),               # 精度取整
    ({**_T, "stop_loss_abs": 90.1, "enter_tag": "manual"}, None, "unknown"),        # 无计划：未知，不用兜底价
    ({**_T, "stop_loss_abs": 90.1, "fw_init_risk": 1.0}, -2.0, "fw_init_risk"),     # 框架记录的初始风险优先
    ({**_T, "stop_loss_abs": 90.1, "orders": [{"ft_is_entry": True, "amount": 1.0, "filled": 0.4}]},
     -5.0, "plan"),                                                                 # 申请 1、成交 0.4：按成交量
    ({**_T, "stop_loss_abs": 90.1, "enter_tag": "fw|s=99.5|c=|t=1"}, -2.0 / 0.8, "plan"),   # floor 模式：抬到 0.8%
])
def test_r_uses_the_framework_stop_not_the_fallback(trade, expect_r, src):
    r, s = r_multiple(trade)
    assert s == src and (r is None if expect_r is None else abs(r - expect_r) < 1e-9), (r, s)


def test_review_and_research_agree():
    import sys as _s
    _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "research"))
    import matrix

    t = {**_T, "stop_loss_abs": 90.1, "trade_id": 3, "open_date": "2026-10-06T00:15:00+00:00"}
    key = ("SOL/USDT:USDT", int(__import__("pandas").Timestamp("2026-10-06T00:15:00+00:00").timestamp()))
    assert from_freqtrade([t], {3: 1.0})[0]["r"] == -2.0 and abs(matrix.r_multiple(t, {key: {"init_risk": 1.0}}) + 2.0) < 1e-9
    assert from_freqtrade([t], None, {"stop_min_pct": 0.8, "stop_max_pct": 3.0})[0]["r_est"] == -2.0
    assert initial_risk(t)[0] == 1.0



def test_risk_uses_the_strategy_stop_limits_and_records():
    """审查 v2.10 P1-2：上下限写死 0.8% / 3%；策略允许 5% 时，4% 的真实止损被判为未知。"""
    t = {**_T, "stop_loss_abs": 96.0, "enter_tag": "fw|s=96|c=|t=1", "profit_abs": -4.0}
    assert initial_risk(t)[0] is None                                              # 默认上限 3%：未知
    assert initial_risk(t, stop_max_pct=5.0) == (4.0, "stop_loss_abs")             # 传入策略实际的上限
    assert initial_risk(t, recorded={"init_risk": 4.0}) == (4.0, "recorded")
    assert initial_risk(t, recorded={"init_risk": None}) == (None, "unknown")
    assert initial_risk(t, stop_max_pct=5.0, require_record=True) == (None, "no_record")   # 研究端：缺记录即未知


def test_review_reads_initial_risk_from_the_freqtrade_db(tmp_path):
    import sqlite3

    from review_metrics import load_init_risk_db

    db = tmp_path / "tradesv3.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trade_custom_data (id INTEGER, ft_trade_id INTEGER, cd_key TEXT, cd_type TEXT, cd_value TEXT)")
    con.execute("INSERT INTO trade_custom_data VALUES (1, 7, 'fw_init_risk', 'float', '1.0')")
    con.commit()
    con.close()
    risk, err = load_init_risk_db(str(db))
    assert risk == {7: 1.0} and err is None
    none, err2 = load_init_risk_db(str(tmp_path / "none.sqlite"))
    assert none == {} and "不存在" in err2                                         # 读不到：给出原因
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert "读取失败" in load_init_risk_db(str(empty))[1]                           # 没有表
    t = {**_T, "trade_id": 7, "stop_loss_abs": 90.1, "enter_tag": "manual"}         # 没有计划、引擎止损是兜底价
    rows = from_freqtrade([t], risk)
    assert rows[0]["r"] == -2.0 and rows[0]["risk_source"] == "recorded"
    m = compute(from_freqtrade([t]))
    assert m["total"]["r_unknown"] == 1 and m["total"]["r_unknown_pnl"] == -2.0


def test_review_never_mixes_estimates_with_recorded_risk():
    """审查 v2.11 P2-1：缺数据库记录时用默认 0.8% / 3% 推算，并与有记录的交易混在一起。"""
    from review_metrics import to_markdown

    t = {**_T, "stop_loss_abs": 96.0, "enter_tag": "fw|s=96|c=|t=1", "profit_abs": -1.0, "trade_id": 1}
    u = {**t, "trade_id": 2, "profit_abs": -2.0}
    params = {"stop_min_pct": 0.8, "stop_max_pct": 5.0}                             # 策略实际允许 5%
    rows = from_freqtrade([t, u], {1: 2.02}, params)
    assert rows[0]["r"] == round(-1.0 / 2.02, 3) and rows[1]["r"] is None and rows[1]["r_est"] == -0.5
    m = compute(rows)
    assert m["total"]["expectancy_r"] == round(-1.0 / 2.02, 3)                       # 主期望值只含有记录的交易
    assert m["total"]["r_estimated"] == 1 and m["total"]["expectancy_r_est_only"] == -0.5
    assert from_freqtrade([u], {}, {"stop_min_pct": 0.8, "stop_max_pct": 3.0})[0]["r_est"] is None   # 默认 3% 推不出 4% 的止损
    md = to_markdown({**m, "risk_evidence": {"db_error": "数据库不存在：x"}}, "t")
    assert "估算" in md and "数据库不存在" in md


def test_review_reads_the_strategy_stop_params(tmp_path):
    import shutil

    from review_metrics import strategy_stop_params

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = tmp_path / "strategies"
    d.mkdir()
    shutil.copy(os.path.join(root, "user_data", "strategies", "framework_base.py"), d)
    (d / "Wide.py").write_text("from framework_base import FrameworkStrategy\n"
                               "class Wide(FrameworkStrategy):\n    framework_params = {'stop_max_pct': 5.0}\n", "utf-8")
    p, err = strategy_stop_params(str(d), "Wide")
    assert err is None and p["stop_max_pct"] == 5.0 and p["stop_min_pct"] == 0.8
    assert strategy_stop_params(str(d), "Nope")[0] is None


@pytest.mark.parametrize("trade,expect_r,src", [
    ({**_T, "is_short": True, "enter_tag": "fw|s=101|c=|t=1", "stop_loss_abs": 101.0, "profit_abs": -1.0}, -1.0, "stop_loss_abs"),
    ({**_T, "is_short": True, "enter_tag": "fw|s=101|c=|t=1", "stop_loss_abs": 109.9, "profit_abs": -2.0}, -2.0, "plan"),   # 兜底价在上方
    ({**_T, "is_short": True, "enter_tag": "fw|s=101|c=|t=1", "stop_loss_abs": 99.0, "profit_abs": -1.0}, -1.0, "plan"),     # 引擎止损在错的一侧：改用计划
    ({**_T, "is_short": True, "enter_tag": "fw|s=100.5|c=|t=1", "stop_loss_abs": 109.9, "profit_abs": -0.8}, -1.0, "plan"),  # floor：0.8%
])
def test_short_r_uses_the_stop_above_entry(trade, expect_r, src):
    """v2.13：空单的止损在入场价上方；兜底止损也在上方很远。"""
    r, s = r_multiple(trade)
    assert s == src and (r is None if expect_r is None else abs(r - expect_r) < 1e-9), (r, s)
    assert r_multiple({**_T, "is_short": True, "enter_tag": "manual", "stop_loss_abs": 99.0})[1] == "unknown"   # 都不行：未知
