"""freq-trader 的研究前端接入：逐笔 R 口径、s03ft lab 的权限边界、默认数据目录。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

import matrix  # noqa: E402


def test_r_multiple_uses_entry_fill_and_stop_loss_abs():
    row = pd.Series({"open_rate": 100.0, "stop_loss_abs": 98.0, "amount": 5.0, "profit_abs": 20.0,
                     "orders": [{"amount": 10.0, "ft_is_entry": True}, {"amount": 5.0, "ft_is_entry": False}]})
    assert matrix.r_multiple(row) == 1.0                       # 风险 = 10 × 2 = 20，盈利 20 → 1R（分批后 amount 只剩 5）
    row["stop_loss_abs"] = 100.0
    assert matrix.r_multiple(row) is None


def test_trades_frame_schema():
    df = matrix.trades_frame("SOL-2024-base", None)
    assert list(df.columns) == ["job", "coin", "year", "variant", "entry_time", "time", "R", "pnl", "risk", "risk_source", "is_short"]


def _s03ft(*args, env=None):
    return subprocess.run([sys.executable, "-m", "s03ft.cli", *args], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=300)


def test_agent_cannot_approve_ideas_through_s03ft():
    r = _s03ft("lab", "idea", "move", "--id", "I-20260101-001", "--to", "approved", "--reason", "自批", "--by", "human")
    assert r.returncode == 2 and "只能由人" in r.stdout
    r = _s03ft("lab", "idea", "move", "--id", "I-20260101-001", "--to", "approved", "--reason", "x", "--approver", "a")
    assert r.returncode == 2


@pytest.mark.skipif(not (ROOT / "user_data/data/okx/futures/BTC_USDT_USDT-1d-futures.feather").exists(),
                    reason="需要本地 OKX 测试数据（freqtrade download-data 生成，不随仓库提交）")
def test_lab_factors_reads_freqtrade_data_dir(tmp_path):
    r = _s03ft("lab", "factors", "--coins", "BTC,SOL", "--ids", "q158_roc20,q158_kmid", "--out-dir", str(tmp_path),
               env={"S03_FT_CONFIG": "user_data/config-test-okx.json"})
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["mode"] == "time_series" and Path(out["markdown"]).exists()


def test_status_reports_news_guard_and_lab_news(tmp_path):
    r = _s03ft("lab", "news", "blocks", "--guard-out", str(tmp_path / "none.json"))
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "disabled"
    sys.path.insert(0, str(ROOT))
    from s03ft import cli

    assert cli.news_info(tmp_path / "none.json")["status"] == "disabled"


# ------------------------------------------------------------------ v2.7：必需压力方案随策略声明
def test_required_stress_follows_the_strategy(tmp_path, monkeypatch):
    sdir = tmp_path / "strats"
    sdir.mkdir()
    (sdir / "MarketEntry.py").write_text(
        "from framework_base import FrameworkStrategy\n"
        "class MarketEntry(FrameworkStrategy):\n"
        "    timeframe = '15m'\n"
        "    research_stress = ('cost2x', 'stopslip')     # 市价入场：不需要 delay1\n"
        "    research_stress_waivers = {'delay1': '只用市价单入场，没有排队等待'}\n"
        "    research_stop_slip = 0.003\n", "utf-8")
    (sdir / "NoReason.py").write_text(
        "from framework_base import FrameworkStrategy\n"
        "class NoReason(FrameworkStrategy):\n"
        "    research_stress = ('cost2x', 'stopslip')\n", "utf-8")
    (sdir / "Empty.py").write_text(
        "from framework_base import FrameworkStrategy\n"
        "class Empty(FrameworkStrategy):\n"
        "    research_stress = ()\n"
        "    research_stress_waivers = {'cost2x': '不需要', 'delay1': '不需要', 'stopslip': '不需要'}\n", "utf-8")
    (sdir / "Bad.py").write_text(
        "from framework_base import FrameworkStrategy\n"
        "class Bad(FrameworkStrategy):\n"
        "    research_stress = ('cost2x', 'moon')\n", "utf-8")
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("FW_STRATEGY_PATH", str(sdir))
    monkeypatch.setenv("FW_STRATEGY", "MarketEntry")
    req = matrix.strategy_requirements()
    assert req["required_stress"] == ["cost2x", "stopslip"] and req["stop_slip"] == 0.003
    assert req["stress_waivers"] == {"delay1": "只用市价单入场，没有排队等待"} and req["strategy_file_sha"]
    monkeypatch.setenv("FW_STRATEGY", "NoReason")                              # 去掉 delay1 没写理由
    with pytest.raises(SystemExit, match="理由"):
        matrix.strategy_requirements()
    monkeypatch.setenv("FW_STRATEGY", "Empty")                                 # 审查 v2.8 P2-2：空元组绕过全部压力测试
    with pytest.raises(SystemExit, match="框架要求"):
        matrix.strategy_requirements()
    monkeypatch.setenv("FW_STRATEGY", "Bad")
    with pytest.raises(SystemExit, match="moon"):
        matrix.strategy_requirements()
    monkeypatch.setenv("FW_STRATEGY_PATH", str(ROOT / "examples" / "strategies"))
    monkeypatch.setenv("FW_STRATEGY", "SmaCrossExample")
    assert matrix.strategy_requirements()["required_stress"] == ["cost2x", "delay1", "stopslip"]   # 框架默认


REQ = {"strategy": "S", "required_stress": ["cost2x"], "stop_slip": 0.002, "timeframe": "15m",
       "research_params": {"stop_min_mode": "skip", "research_entry_delay_bars": 0, "better_fill_policy": "widen"}}


@pytest.fixture
def pf(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "PORTFOLIO", tmp_path / "portfolio")
    monkeypatch.setattr(matrix, "exchange_and_pairs", lambda: ("okx", ["BTC/USDT:USDT"]))
    return tmp_path / "portfolio"


def test_portfolio_failure_replaces_latest(pf, monkeypatch):
    """审查 v2.6 P1-4：失败的运行也要成为 latest（状态 error），旧的成功结果不能再被读到。"""
    monkeypatch.setattr(matrix, "run_freqtrade", lambda v, tr, p, o: ({"status": "error", "stderr_tail": "boom"}, None))
    res = matrix.portfolio("base", "20240101-", {"id": "fp"}, REQ)
    latest = json.loads((pf / "base" / "latest.json").read_text("utf-8"))
    assert res["status"] == "error" and latest["status"] == "error" and "csv" not in latest


def test_portfolio_exception_never_leaves_the_old_latest(pf, monkeypatch):
    """审查 v2.7 P1-4：运行中途抛异常（读结果、算统计出错）时，latest 仍指向上一次成功的结果。"""
    (pf / "base").mkdir(parents=True)
    (pf / "base" / "latest.json").write_text(json.dumps({"status": "ok", "csv": "run-old/trades.csv"}))

    def boom(*a):
        raise RuntimeError("load_backtest_data failed")
    monkeypatch.setattr(matrix, "run_freqtrade", boom)
    res = matrix.portfolio("base", "20240101-", {"id": "fp"}, REQ)
    latest = json.loads((pf / "base" / "latest.json").read_text("utf-8"))
    assert res["status"] == latest["status"] == "error" and "RuntimeError" in latest["error"]
    assert latest["experiment_id"] == matrix.experiment_id("fp", REQ, "20240101-", ["BTC/USDT:USDT"]) and latest["started_at"]

    seen = {}

    def interrupted(*a):
        seen["latest"] = json.loads((pf / "base" / "latest.json").read_text("utf-8"))["status"]
        raise KeyboardInterrupt
    monkeypatch.setattr(matrix, "run_freqtrade", interrupted)
    with pytest.raises(KeyboardInterrupt):
        matrix.portfolio("base", "20240101-", {"id": "fp"}, REQ)
    assert seen["latest"] == "running"                                          # 运行期间 latest 不是旧的成功结果
    assert json.loads((pf / "base" / "latest.json").read_text("utf-8"))["status"] == "error"


def test_variant_that_equals_base_is_refused(pf, monkeypatch):
    """审查 v2.7 P2：策略的 framework_params 已经是 floor 时，stop_floor 方案与 base 相同（v2.7 照样出结果）。"""
    called = []
    monkeypatch.setattr(matrix, "run_freqtrade", lambda *a: called.append(a) or ({"status": "ok"}, None))
    floor = {**REQ, "research_params": {**REQ["research_params"], "stop_min_mode": "floor"}}
    res = matrix.portfolio("stop_floor", "20240101-", {"id": "fp"}, floor)
    assert res["status"] == "error" and "相同" in res["error"] and not called
    assert matrix.variant_problem("stop_floor", REQ) is None
    assert matrix.variant_problem("cost2x", floor) is None                     # 手续费方案总是不同
    env = matrix.research_env("stop_floor")
    assert json.loads(env["S03_RESEARCH_OVERRIDES"]) == {"stop_min_mode": "floor"}
    assert matrix.research_env("base")["S03_RESEARCH_OVERRIDES"] == ""


def test_experiment_id_changes_with_timerange_pairs_and_strategy():
    a = matrix.experiment_id("fp", REQ, "20220101-", ["BTC/USDT:USDT"])
    assert a != matrix.experiment_id("fp", REQ, "20230101-", ["BTC/USDT:USDT"])
    assert a != matrix.experiment_id("fp", REQ, "20220101-", ["BTC/USDT:USDT", "ETH/USDT:USDT"])
    assert a != matrix.experiment_id("fp", {**REQ, "strategy": "T"}, "20220101-", ["BTC/USDT:USDT"])
    assert a == matrix.experiment_id("fp", REQ, "20220101-", ["BTC/USDT:USDT"])


def _feather(d, name, start, end, freq, drop=()):
    idx = pd.date_range(start, end, freq=freq, tz="UTC")
    idx = idx[~idx.isin(pd.DatetimeIndex(drop))] if len(drop) else idx
    pd.DataFrame({"date": idx, "open": 1.0}).to_feather(d / name)


def test_data_gaps_checks_end_and_internal_gaps(tmp_path):
    """审查 v2.7 P1-6：只检查第一天。现在查起点、终点、内部缺口、比其他交易对早结束。"""
    d = tmp_path / "okx" / "futures"
    d.mkdir(parents=True)
    for b in ("BTC", "ETH"):
        n = f"{b}_USDT_USDT"
        _feather(d, f"{n}-1d-futures.feather", "2023-12-01", "2024-12-31", "1D")
        _feather(d, f"{n}-15m-futures.feather", "2024-01-01", "2024-12-31", "15min")
        _feather(d, f"{n}-1h-mark.feather", "2024-01-01", "2024-12-31", "1h")
        _feather(d, f"{n}-1h-funding_rate.feather", "2024-01-01", "2024-12-31", "8h")
    kw = dict(timeframe="15m", data_dir=tmp_path, exchange="okx", pairs=["BTC/USDT:USDT", "ETH/USDT:USDT"])
    assert matrix.data_gaps("20240101-20241231", **kw) == []
    _feather(d, "ETH_USDT_USDT-1h-funding_rate.feather", "2024-01-01", "2024-12-31", "8h",
             drop=pd.date_range("2024-05-01", "2024-05-04", freq="8h", tz="UTC"))     # 中间缺 4 天资金费（v2.7 查不到）
    g = matrix.data_gaps("20240101-20241231", **kw)
    assert len(g) == 1 and "ETH" in g[0] and "资金费率" in g[0] and "区间内有缺口" in g[0]
    _feather(d, "BTC_USDT_USDT-1d-futures.feather", "2023-12-01", "2024-09-30", "1D")   # 日线提前结束
    g = matrix.data_gaps("20240101-20241231", **kw)
    assert any("BTC" in x and "日线" in x and "就没有了" in x for x in g)
    assert any("BTC" in x and "日线" in x and "早结束" in x for x in matrix.data_gaps("20240101-", **kw))
    assert any("从 2024-01-01" in x for x in matrix.data_gaps("20231201-20241231", **kw))   # 起点之前没有数据


def test_runs_are_recorded_in_the_idea_book(pf, monkeypatch, tmp_path):
    """审查 v2.8 P1-1：研究脚本不写想法登记簿，系统不知道这个想法已经被回测过。"""
    sys.path.insert(0, str(ROOT))
    from fwresearch.ideas import IdeaBook

    ideas = tmp_path / "ideas.jsonl"
    monkeypatch.setattr(matrix, "IDEAS", ideas)
    book = IdeaBook(ideas)
    it = book.add("测试想法", "某因子预测收益", "资金拥挤", "样本外不显著", "human")
    with pytest.raises(SystemExit, match="--idea"):
        matrix.check_idea("")
    with pytest.raises(SystemExit, match="没有"):
        matrix.check_idea("I-20990101-001")
    monkeypatch.setattr(matrix, "run_freqtrade", lambda *a: ({"status": "error", "stderr_tail": "x"}, None))
    matrix.portfolio("base", "20240101-", {"id": "fp", "config": "c"}, REQ, it.id)
    matrix.portfolio("base", "20240101-", {"id": "fp", "config": "c"}, REQ, it.id)          # 原样重跑
    matrix.portfolio("cost2x", "20240101-", {"id": "fp", "config": "c"}, REQ, it.id)
    got = IdeaBook(ideas).get(it.id)
    assert len(got.backtests) == 3 and got.first_backtest_at and got.n_configs() == 1    # 重跑、压力方案不算新尝试
    with pytest.raises(ValueError, match="回测之前"):
        IdeaBook(ideas).set_holdout(it.id, "2025-01-01")
    latest = json.loads((pf / "base" / "latest.json").read_text("utf-8"))
    assert latest["idea"] == it.id and latest["experiment_id"] == matrix.experiment_id("fp", REQ, "20240101-",
                                                                                         ["BTC/USDT:USDT"], it.id)


def test_plan_refuses_variants_equal_to_base(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    from fwresearch.ideas import IdeaBook

    ideas = tmp_path / "ideas.jsonl"
    monkeypatch.setattr(matrix, "IDEAS", ideas)
    it = IdeaBook(ideas).add("测试想法", "某因子预测收益", "资金拥挤", "样本外不显著", "human")
    floor = {**REQ, "research_params": {**REQ["research_params"], "stop_min_mode": "floor"}}
    monkeypatch.setattr(matrix, "strategy_requirements", lambda: floor)
    monkeypatch.setattr(sys, "argv", ["matrix.py", "plan", "--pairs", "BTC", "--years", "2024-2024",
                                      "--variants", "base,stop_floor", "--idea", it.id])
    with pytest.raises(SystemExit, match="相同"):
        matrix.main()



def test_fingerprint_covers_the_risk_module_and_every_imported_repo_module(monkeypatch):
    """审查 v2.10 P1-3：monitor/trade_risk.py 不在版本指纹里，改了 R 的公式旧结果仍算当前版本。"""
    monkeypatch.chdir(ROOT)
    files = matrix.fingerprint_files()
    assert "monitor/trade_risk.py" in files
    import fwresearch.__main__  # noqa: F401  研究流程会导入的模块
    import fwresearch.ideas  # noqa: F401
    import fwresearch.stats  # noqa: F401
    imported = {str(m) for m in matrix.repo_modules()}
    research = {f for f in imported if f.startswith(("research/", "fwresearch/", "monitor/trade_risk", "user_data/"))}
    assert research <= set(files), research - set(files)
    before = matrix._sha(files)
    assert matrix._sha(files) == before                                          # 原样：稳定
    monkeypatch.setattr(matrix.Path, "read_bytes",
                        lambda self, _o=matrix.Path.read_bytes: _o(self) + (b"#changed" if self.name == "trade_risk.py" else b""))
    assert matrix._sha(files) != before                                          # 只改风险模块：指纹变化


def test_research_r_uses_recorded_risk_and_marks_missing_records_unknown():
    import pandas as pd

    t = pd.DataFrame([{"pair": "SOL/USDT:USDT", "open_date": pd.Timestamp("2026-10-06 00:15", tz="UTC"),
                       "close_date": pd.Timestamp("2026-10-07", tz="UTC"), "open_rate": 100.0, "stop_loss_abs": 90.1,
                       "profit_abs": -2.0, "amount": 1.0, "enter_tag": "manual",
                       "orders": [{"ft_is_entry": True, "amount": 1.0, "filled": 1.0}]}])
    key = ("SOL/USDT:USDT", int(pd.Timestamp("2026-10-06 00:15", tz="UTC").timestamp()))
    f = matrix.trades_frame("SOL-2026-base", t, {key: {"init_risk": 1.0}})
    assert f["R"].tolist() == [-2.0]
    assert matrix.trades_frame("SOL-2026-base", t, {})["R"].isna().all()        # 缺记录 → 未知（不再事后推算）



def test_verify_compares_final_r_not_only_the_fills(pf, monkeypatch):
    """审查 v2.11 P2-2：verify 只比 trades_hash 与指纹；成交相同而未知 R 从 0 变成 300 笔，仍报告 match=true。"""
    base = {"status": "ok", "trades_hash": "t1", "fingerprint": "fp", "result_hash": "r1", "n_r_unknown": 0}
    assert matrix.compare_runs(base, dict(base))["status"] == "完整复现"
    assert matrix.compare_runs(base, {**base, "result_hash": "r2"})["status"] == "不一致"            # 风险或 R 变了
    c = matrix.compare_runs(base, {**base, "result_hash": "r2", "n_r_unknown": 300})
    assert c["status"] == "成交一致、风险证据不完整" and c["match"] is False
    assert matrix.compare_runs({**base, "result_hash": None}, base)["match"] is False                # 旧结果没有结果哈希
    import pandas as pd
    tf = pd.DataFrame({"coin": ["SOL"], "entry_time": ["2026-10-06"], "time": ["2026-10-07"], "risk": [1.0], "R": [-2.0],
                       "risk_source": ["recorded"], "pnl": [-2.0]})
    assert matrix.result_hash(tf) != matrix.result_hash(tf.assign(risk=[2.0], R=[-1.0]))
    assert matrix.result_hash(tf) != matrix.result_hash(tf.assign(R=[None], risk=[None], risk_source=["no_record"]))


def test_portfolio_verify_reruns_without_touching_latest(pf, monkeypatch, capsys):
    import pandas as pd

    monkeypatch.setattr(matrix, "strategy_requirements", lambda: REQ)
    monkeypatch.setattr(matrix, "fingerprint", lambda: {"id": "fp", "config": "c"})
    monkeypatch.setattr(matrix, "data_gaps", lambda *a, **k: [])
    monkeypatch.setattr(matrix, "resolved_config", lambda: ("{}", {}))
    trades = pd.DataFrame([{"pair": "SOL/USDT:USDT", "open_date": pd.Timestamp("2026-10-06 00:15", tz="UTC"),
                            "close_date": pd.Timestamp("2026-10-07", tz="UTC"), "open_rate": 100.0, "stop_loss_abs": 99.0,
                            "profit_abs": -1.0, "amount": 1.0, "enter_tag": "fw|s=99|c=|t=1", "exit_reason": "stop_loss",
                            "orders": [{"ft_is_entry": True, "amount": 1.0, "filled": 1.0}]}])
    key = {"pair": "SOL/USDT:USDT", "open_ts": int(pd.Timestamp("2026-10-06 00:15", tz="UTC").timestamp()), "init_risk": 1.0}

    def fake(variant, tr, pairs, outdir, rec=[key]):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "risk.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rec))
        return {"status": "ok", "risk_log": str(outdir / "risk.jsonl")}, trades
    monkeypatch.setattr(matrix, "run_freqtrade", fake)
    matrix.portfolio("base", "20261001-", None, REQ, "")
    latest = (pf / "base" / "latest.json").read_text("utf-8")
    monkeypatch.setattr(sys, "argv", ["matrix.py", "portfolio-verify", "--variant", "base"])
    assert matrix.main() == 0 and json.loads(capsys.readouterr().out.strip())["status"] == "完整复现"
    monkeypatch.setattr(matrix, "run_freqtrade", lambda v, tr, p, o: fake(v, tr, p, o, rec=[]))      # 重跑时缺风险记录
    assert matrix.main() == 1 and json.loads(capsys.readouterr().out.strip())["status"] == "成交一致、风险证据不完整"
    assert (pf / "base" / "latest.json").read_text("utf-8") == latest                              # latest 没被改


def test_fingerprint_paths_are_posix(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert all("\\" not in f for f in matrix.fingerprint_files())
