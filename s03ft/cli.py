"""s03ft：S03 freqtrade 流程的统一命令行（OpenClaw agent 通过它执行全部操作）。

  s03ft doctor [--json]                       环境自检（部署后、每日例行都可运行）
  s03ft status [--json]                       服务状态、代理状态、事件文件新鲜度、最新复盘指标
  s03ft daily [--json]                        每日例行：更新事件日历 → 导出复盘数据 → 汇总（给 s03-ops）
  s03ft svc start|stop|restart freqtrade|monitor|gate
  s03ft job events|export                     运行一次性任务
  s03ft logs freqtrade|monitor|gate           最近 200 行日志
  s03ft gate status | gate pause <原因>       白名单代理：查看状态 / 暂停新开仓
  s03ft events build --out FILE [--history]   从美联储、BLS、BEA 官方日程生成事件文件
  s03ft research plan|run|verify|aggregate|portfolio|portfolio-verify|verdict|g0 [参数…]   研究矩阵与组合回测（freqtrade）
  s03ft lab factors|stats|matrix|idea [参数…]  研究前端：筛因子、统计检验、想法登记簿（docs/RESEARCH_LAB.md）
  s03ft lab vt-check [--vt-bin DIR]           Vibe-Trading 安装自检（默认 /opt/s03/vt-venv/bin）
  s03ft lab news check|blocks|daily|backfill|replay …  新闻（Odaily + BlockBeats）：自检、当前守卫、每日汇总、历史回填、守卫回放
  s03ft metrics [--json]                      读取程序算好的复盘指标
  s03ft test                                  运行测试

没有恢复交易、强制开平仓、修改配置、切换 dry_run 的命令：这些只能由人操作。
环境变量：
  S03FT_HOME        代码目录（默认：本包所在仓库）
  S03FT_INBOX       复盘收件目录（默认 /srv/s03-review/inbox）
  S03_EVENTS_FILE   事件文件（默认 /var/lib/s03-events/events.json）
  S03_GATE_URL      白名单代理（默认 http://127.0.0.1:8765）
  S03_GATE_TOKEN_FILE  代理 token 文件（默认 /etc/s03/openclaw/gate.token）
  S03FT_DRY_RUN=1   只打印将执行的系统命令，不执行（测试用）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("S03FT_HOME") or Path(__file__).resolve().parents[1])
INBOX = Path(os.environ.get("S03FT_INBOX", "/srv/s03-review/inbox"))
EVENTS = Path(os.environ.get("S03_EVENTS_FILE", "/var/lib/s03-events/events.json"))
GATE_URL = os.environ.get("S03_GATE_URL", "http://127.0.0.1:8765")
GATE_TOKEN_FILE = os.environ.get("S03_GATE_TOKEN_FILE", "/etc/s03/openclaw/gate.token")

UNITS = {"freqtrade": "s03-freqtrade.service", "monitor": "s03-ft-monitor.service", "gate": "s03-ft-gate.service"}
JOBS = {"events": "s03-events.service", "export": "s03-ft-export.service"}
SYSTEMCTL, JOURNALCTL, SUDO = "/usr/bin/systemctl", "/usr/bin/journalctl", "/usr/bin/sudo"
EVENTS_MAX_AGE_H = 72


# ---------------------------------------------------------------------- 基础
def dry() -> bool:
    return os.environ.get("S03FT_DRY_RUN") == "1"


def run(cmd: list[str], timeout: int = 600, check: bool = False) -> dict:
    """执行固定的系统命令。sudo 一律加 -n（不交互，不能输密码）。"""
    if dry():
        return {"cmd": cmd, "rc": 0, "out": "", "dry_run": True}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"cmd": cmd, "rc": 127, "out": "", "err": "命令不存在"}
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "rc": 124, "out": "", "err": "超时"}
    res = {"cmd": cmd, "rc": r.returncode, "out": r.stdout[-6000:], "err": r.stderr[-2000:]}
    if check and r.returncode != 0:
        raise SystemExit(emit({"ok": False, **res}, True) or 1)
    return res


def emit(obj, as_json: bool, text: str | None = None) -> int:
    if as_json or text is None:
        print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    else:
        print(text)
    return 0 if (not isinstance(obj, dict) or obj.get("ok", True)) else 1


def unit_state(unit: str) -> str:
    if dry():
        return "unknown(dry-run)"
    if not shutil.which("systemctl") and not Path(SYSTEMCTL).exists():
        return "unknown(no-systemd)"
    r = subprocess.run([SYSTEMCTL, "is-active", unit], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def gate_call(action: str, reason: str = "") -> dict:
    if not GATE_URL.startswith(("http://127.0.0.1", "http://localhost")):
        return {"ok": False, "error": "只允许本机代理地址"}
    if dry():
        return {"ok": True, "dry_run": True, "action": action}
    try:
        token = Path(GATE_TOKEN_FILE).read_text("utf-8").strip()
    except OSError as e:
        return {"ok": False, "error": f"读取代理 token 失败：{type(e).__name__}"}
    if action == "status":
        req = urllib.request.Request(GATE_URL + "/status", method="GET")
    else:
        req = urllib.request.Request(GATE_URL + "/pause", method="POST",
                                     data=json.dumps({"reason": reason[:200] or "openclaw"}, ensure_ascii=False).encode())
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"ok": True, **json.loads(r.read().decode() or "{}")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "error": f"代理不可用：{type(e).__name__}"}


NEWS_FILE = Path(os.environ.get("S03_NEWS_FILE", "/var/lib/s03-news/news_blocks.json"))


def news_info(path: Path = NEWS_FILE) -> dict:
    """新闻事件守卫的状态：disabled（未启用）/ ok / degraded（一个来源失败，封禁照常）/ stale（过期，交易程序已忽略）/ error。"""
    from fwresearch.blockbeats import load_blocks

    blocks, status = load_blocks(str(path))
    try:
        sources = json.loads(path.read_text("utf-8")).get("sources", {})
    except (OSError, ValueError):
        sources = {}
    return {"status": status, "file": str(path), "sources": sources,
            "blocks": [{k: b[k] for k in ("target", "category", "until_utc", "title")} for b in blocks]}


def events_info(path: Path = EVENTS) -> dict:
    if not path.exists():
        return {"ok": False, "path": str(path), "error": "事件文件不存在（交易程序会拒绝新开仓）"}
    age_h = (time.time() - path.stat().st_mtime) / 3600
    try:
        d = json.loads(path.read_text("utf-8"))
    except ValueError:
        return {"ok": False, "path": str(path), "error": "事件文件不是有效 JSON"}
    upcoming = [e for e in d.get("events", []) if e.get("time_utc", "") >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())]
    return {"ok": age_h <= EVENTS_MAX_AGE_H and bool(d.get("ok", False)), "age_hours": round(age_h, 1),
            "sources": {k: {"ok": v.get("ok"), "issues": v.get("issues")} for k, v in d.get("sources", {}).items()},
            "next_events": [{"time_utc": e["time_utc"], "kind": e["kind"]} for e in upcoming[:6]],
            "changes": [{"type": c["type"], "kind": c["kind"], "time_utc": c["time_utc"],
                         "old_time_utc": c.get("old_time_utc")} for c in d.get("changes", [])]}


def metrics_info(inbox: Path = INBOX) -> dict:
    p = inbox / "metrics.json"
    if not p.exists():
        return {"ok": False, "error": f"{p} 不存在（先运行 s03ft job export）"}
    try:
        m = json.loads(p.read_text("utf-8"))
    except ValueError:
        return {"ok": False, "error": "metrics.json 无效"}
    return {"ok": "error" not in m, "total": m.get("total"), "by_coin": m.get("by_coin"),
            "age_hours": round((time.time() - p.stat().st_mtime) / 3600, 1)}


def alerts_24h(inbox: Path = INBOX) -> dict:
    p = inbox / "alerts.jsonl"
    if not p.exists():
        return {}
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    counts: dict[str, int] = {}
    for line in p.read_text("utf-8").splitlines()[-5000:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("kind") == "alert" and str(r.get("ts", "")) >= since:
            k = f"{r.get('severity')}:{r.get('rule')}"
            counts[k] = counts.get(k, 0) + 1
    return counts


# ---------------------------------------------------------------------- 子命令
def strategy_info(research: bool = False) -> dict:
    """读取配置里的策略名，找到策略文件，判断是否仍是空白模板（is_template = True）。"""
    cfg_paths = [HOME / "user_data" / "config.json"] if research else [Path("/etc/s03/ft-config.json"),
                                                                       HOME / "user_data" / "config.json"]
    name = os.environ.get("FW_STRATEGY")
    for p in cfg_paths:
        if name:
            break
        try:
            name = json.loads(p.read_text("utf-8")).get("strategy")
        except (OSError, ValueError):
            continue
    name = name or "MyStrategy"
    found = None
    for d in (HOME / "user_data" / "strategies", HOME / "examples" / "strategies"):
        for f in d.glob("*.py") if d.exists() else []:
            if f"class {name}(" in f.read_text("utf-8", errors="ignore"):
                found = f
                break
        if found:
            break
    is_tpl = bool(found) and "is_template = True" in found.read_text("utf-8", errors="ignore")
    return {"name": name, "file": str(found) if found else None, "is_template": is_tpl}


def cmd_doctor(a) -> int:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        import freqtrade  # noqa: F401
        from freqtrade import __version__ as ftv
        add("freqtrade 已安装且为 2026.10", ftv == "2026.10", ftv)
    except ImportError:
        add("freqtrade 已安装且为 2026.10", a.research, "未安装（运维与告警 agent 不需要）")
    info = strategy_info(research=a.research)
    add("策略文件存在", info["file"] is not None, info.get("file") or f"找不到策略 {info['name']}")
    add("已配置自己的策略（不是空白模板）", info["file"] is not None and not info["is_template"],
        "仍是空白模板：不会产生任何入场，见 docs/STRATEGY_TEMPLATE.md" if info["is_template"] else info["name"])
    # 周期只能由策略决定：配置里的 timeframe 会覆盖策略（freqtrade 规定配置优先），v2.5 之前的配置写死了 1m
    cfg_files = [HOME / "user_data" / "config.json"] if a.research else [Path("/etc/s03/ft-config.json")]
    for cf in cfg_files:
        try:
            tf = json.loads(cf.read_text("utf-8")).get("timeframe")
        except (OSError, ValueError):
            continue
        add(f"{cf.name} 没有写 timeframe（周期以策略为准）", tf is None,
            f"配置写了 timeframe = {tf}，会覆盖策略里的周期：删掉这一行" if tf else "")
    for f in ("monitor/events_calendar.py", "monitor/run_monitor.py", "research/matrix.py"):
        add(f"{f} 存在", (HOME / f).exists())
    if not a.research:
        cfg = Path("/etc/s03/ft-config.json")
        if cfg.exists():
            try:
                dr = json.loads(cfg.read_text("utf-8")).get("dry_run")
                add("ft-config.json 可读", True, f"dry_run = {dr}")
            except ValueError:
                add("ft-config.json 可读", False, "JSON 无效")
        else:
            add("ft-config.json 可读", False, "/etc/s03/ft-config.json 不存在")
        add("/etc/s03 下的密钥文件对当前用户不可读", not os.access("/etc/s03/ft-trader.env", os.R_OK),
            "可读则说明权限配置错误" if os.access("/etc/s03/ft-trader.env", os.R_OK) else "")
        add("代理 token 可读", os.access(GATE_TOKEN_FILE, os.R_OK), GATE_TOKEN_FILE)
        for k, u in {**UNITS, **{"events-timer": "s03-events.timer", "heartbeat-timer": "s03-heartbeat.timer"}}.items():
            st = unit_state(u)
            add(f"{u} 运行中", st == "active" or st.startswith("unknown"), st)
        ev = events_info()
        add("事件文件新鲜（≤ 72 小时）且各来源正常", ev.get("ok"), json.dumps(ev.get("sources") or ev.get("error"),
                                                                   ensure_ascii=False)[:300])
    ok = all(c["ok"] for c in checks)
    text = "\n".join(f"{'✓' if c['ok'] else '✗'} {c['check']}  {c['detail']}" for c in checks)
    return emit({"ok": ok, "checks": checks}, a.json, text + f"\n\n{'全部通过' if ok else '有未通过项'}")


def cmd_status(a) -> int:
    obj = {"ok": True, "strategy": strategy_info(), "services": {k: unit_state(u) for k, u in UNITS.items()},
           "gate": gate_call("status"), "events": events_info(), "news_guard": news_info(),
           "metrics": metrics_info(), "alerts_24h": alerts_24h()}
    obj["ok"] = all(v == "active" for v in obj["services"].values() if not v.startswith("unknown"))
    return emit(obj, True)


def cmd_svc(a) -> int:
    r = run([SUDO, "-n", SYSTEMCTL, a.action, UNITS[a.name]], timeout=120)
    time.sleep(0 if dry() else 3)
    return emit({"ok": r["rc"] == 0, "unit": UNITS[a.name], "action": a.action, "state": unit_state(UNITS[a.name]),
                 "result": r}, True)


def cmd_job(a) -> int:
    r = run([SUDO, "-n", SYSTEMCTL, "start", JOBS[a.name]], timeout=900)
    return emit({"ok": r["rc"] == 0, "unit": JOBS[a.name], "result": r}, True)


def cmd_logs(a) -> int:
    r = run([SUDO, "-n", JOURNALCTL, "-u", UNITS[a.name], "-n", "200", "--no-pager"], timeout=60)
    return emit({"ok": r["rc"] == 0, "unit": UNITS[a.name], "log": r.get("out", "")}, a.json, r.get("out", "") or r.get("err", ""))


def cmd_gate(a) -> int:
    if a.action == "pause":
        reason = " ".join(a.reason or [])
        if not reason:
            return emit({"ok": False, "error": "必须写明暂停原因"}, True)
        return emit(gate_call("pause", reason), True)
    return emit(gate_call("status"), True)


def cmd_daily(a) -> int:
    steps = {"services_before": {k: unit_state(u) for k, u in UNITS.items()}}
    steps["events_job"] = run([SUDO, "-n", SYSTEMCTL, "start", JOBS["events"]], timeout=900)["rc"]
    steps["export_job"] = run([SUDO, "-n", SYSTEMCTL, "start", JOBS["export"]], timeout=900)["rc"]
    summary = {"ok": True, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "strategy": strategy_info(), "steps": steps, "gate": gate_call("status"), "events": events_info(),
               "metrics": metrics_info(), "alerts_24h": alerts_24h()}
    problems = [k for k, v in steps["services_before"].items() if v not in ("active",) and not v.startswith("unknown")]
    if steps["events_job"] != 0:
        problems.append("s03-events 失败（来源异常或格式变化，见 events.sources）")
    if steps["export_job"] != 0:
        problems.append("s03-ft-export 失败")
    if summary["strategy"]["is_template"]:
        problems.append("仍在使用空白策略模板：不会产生任何入场（见 docs/STRATEGY_TEMPLATE.md）")
    if not summary["events"].get("ok"):
        problems.append("事件文件不新鲜或有来源异常")
    summary["problems"] = problems
    summary["ok"] = not problems
    summary["evidence"] = "以上均为程序读取的已核验数据；agent 汇报时不得补充未列出的数字"
    return emit(summary, True)


def cmd_events(a) -> int:
    sys.path.insert(0, str(HOME / "monitor"))
    from events_calendar import main as ev_main

    args = ["--out", a.out] + (["--history"] if a.history else [])
    return ev_main(args)


def cmd_research(a) -> int:
    script = HOME / "research" / "matrix.py"
    r = subprocess.run([sys.executable, str(script), a.sub, *a.args], cwd=os.getcwd())
    return r.returncode


def _ft_data_dir() -> str:
    """config 里的交易所名 → freqtrade 的期货数据目录。"""
    cfg = Path(os.environ.get("S03_FT_CONFIG", "user_data/config.json"))
    try:
        name = json.loads(cfg.read_text("utf-8"))["exchange"]["name"]
    except (OSError, ValueError, KeyError):
        name = "binance"
    return f"user_data/data/{name}/futures"


def cmd_lab(a) -> int:
    """研究前端（fwresearch）。agent 经这里调用：不允许以人的身份批准想法（--by human / --approver）。"""
    args = [a.sub, *a.args]
    if "--by" in args and args[args.index("--by") + 1:args.index("--by") + 2] == ["human"] or "--approver" in args:
        print(json.dumps({"error": "批准想法只能由人在终端用 python -m fwresearch idea move … --by human 执行"},
                         ensure_ascii=False))
        return 2
    if a.sub == "factors" and "--source" not in args:
        args += ["--source", "freqtrade", "--data-dir", _ft_data_dir()]
    from fwresearch.__main__ import main as fw_main

    return fw_main(args)


def cmd_metrics(a) -> int:
    m = metrics_info()
    md = INBOX / "metrics.md"
    return emit(m, a.json, md.read_text("utf-8") if md.exists() else m.get("error"))


def cmd_test(a) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", str(HOME / "tests")], cwd=str(HOME)).returncode


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="s03ft", description="S03 freqtrade 流程命令行（给 OpenClaw agent 使用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor")
    p.add_argument("--json", action="store_true")
    p.add_argument("--research", action="store_true", help="研究沙箱内自检（不检查服务与 /etc/s03）")
    p.set_defaults(fn=cmd_doctor)
    sub.add_parser("status").set_defaults(fn=cmd_status, json=True)
    sub.add_parser("daily").set_defaults(fn=cmd_daily, json=True)
    p = sub.add_parser("svc")
    p.add_argument("action", choices=["start", "stop", "restart"])
    p.add_argument("name", choices=sorted(UNITS))
    p.set_defaults(fn=cmd_svc)
    p = sub.add_parser("job")
    p.add_argument("name", choices=sorted(JOBS))
    p.set_defaults(fn=cmd_job)
    p = sub.add_parser("logs")
    p.add_argument("name", choices=sorted(UNITS))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("gate")
    p.add_argument("action", choices=["status", "pause"])
    p.add_argument("reason", nargs="*")
    p.set_defaults(fn=cmd_gate)
    p = sub.add_parser("events")
    p.add_argument("sub", choices=["build"])
    p.add_argument("--out", required=True)
    p.add_argument("--history", action="store_true")
    p.set_defaults(fn=cmd_events)
    p = sub.add_parser("research")
    p.add_argument("sub", choices=["plan", "run", "verify", "aggregate", "portfolio", "portfolio-verify", "verdict", "fingerprint", "g0"])
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_research)
    p = sub.add_parser("lab")
    p.add_argument("sub", choices=["factors", "stats", "matrix", "idea", "vt-check", "news"])
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_lab)
    p = sub.add_parser("metrics")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_metrics)
    sub.add_parser("test").set_defaults(fn=cmd_test)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if not hasattr(a, "json"):
        a.json = False
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
