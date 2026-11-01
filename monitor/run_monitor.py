"""freqtrade 版监控入口。

  python monitor/run_monitor.py alerts [-c monitor/monitor.toml]   # 行情监控与告警（不需要交易所密钥）
  python monitor/run_monitor.py gate   [-c monitor/monitor.toml]   # OpenClaw 白名单代理：GET /status、POST /pause

环境变量：
  FT_API_USER / FT_API_PASS      freqtrade api_server 的账号（只给本监控进程，绝不给 OpenClaw）
  OPENCLAW_HOOKS_TOKEN           OpenClaw hooks 专用 token（告警发送用）
  OPENCLAW_GATE_TOKEN            白名单代理 token（OpenClaw 调用代理时用；≥ 24 字符）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from binance_monitor import BinanceMonitor, FtClient, Journal, load_config  # noqa: E402


def gate_status(ft: FtClient, journal: Journal) -> dict:
    """只读摘要：不含余额、账户、API 信息。"""
    try:
        cfg = ft.get("show_config")
        trades = ft.get("status")
    except Exception as e:
        return {"error": f"freqtrade API 不可用：{type(e).__name__}"}
    alerts = [r for r in journal.read() if r.get("kind") == "alert"][-20:]
    return {
        "state": cfg.get("state"), "dry_run": cfg.get("dry_run"), "runmode": cfg.get("runmode"),
        "positions": [{"pair": t.get("pair"), "is_short": bool(t.get("is_short")), "open_rate": t.get("open_rate"),
                       "stop_loss_abs": t.get("stop_loss_abs"), "profit_pct": t.get("profit_pct"),
                       "exchange_stop": any(o.get("ft_order_side") == "stoploss" and o.get("is_open")
                                            for o in t.get("orders") or [])}
                      for t in trades if t.get("is_open")],
        "recent_alerts": [{k: a.get(k) for k in ("ts", "coin", "rule", "severity", "detail")} for a in alerts],
    }


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("alerts", "gate"):
        sys.exit(__doc__)
    path = sys.argv[sys.argv.index("-c") + 1] if "-c" in sys.argv else "monitor/monitor.toml"
    cfg = load_config(path)
    if sys.argv[1] == "alerts":
        BinanceMonitor(cfg).run_forever()
    else:
        from gate import serve

        ft, j = FtClient(cfg.ft_api_url), Journal(cfg.journal_path)

        def pause(reason: str) -> None:
            ft.stopentry()                       # freqtrade 自带：只停新开仓，持仓与止损照常管理
            j.write("gate_pause", reason=reason)

        serve(lambda: gate_status(ft, j), pause, log=lambda d: None)


if __name__ == "__main__":
    main()
