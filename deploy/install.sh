#!/usr/bin/env bash
# S-03 freqtrade 一键部署（以 root 在你的 Linux 服务器上运行；可重复运行，不会覆盖已有的密钥与配置）。
#
#   sudo bash deploy/install.sh [--openclaw-user openclaw] [--with-vibe-trading] [--with-news] [--with-blockbeats] [--prefix /tmp/fakeroot]
#
# 做的事：建用户与目录 → 安装 freqtrade 2026.10 与 s03ft → 生成配置与随机 token（只在文件不存在时）
#        → 安装 systemd、sudoers、小工具 → 放置 4 个 OpenClaw agent 的工作区与技能 → enable 定时器 → 自检
# 不做的事：不填交易所 API Key、不启动 freqtrade、不改 dry_run、不修改你的 OpenClaw 主配置。
# --with-vibe-trading：另建 /opt/s03/vt-venv 安装 Vibe-Trading（研究前端找想法用，经 mcp-guard 白名单代理，只给 s03-research）。
# --with-news：启用新闻采集（Odaily + BlockBeats 两家同时；存档 + 按币种的事件守卫，只收紧）和每日数据快照。需要在 /etc/s03/blockbeats.env 填 BlockBeats Key。
# --with-blockbeats：在 --with-news 基础上，再安装官方 BlockBeats MCP（改为 HTTPS）给 s03-research。
# --prefix 只用于测试：把所有文件装到一个假根目录，跳过建用户、chown、systemctl、visudo。
set -euo pipefail

OC_USER="openclaw"
PREFIX=""
WITH_VT=0
WITH_BB=0
WITH_NEWS=0
VT_VERSION="0.1.18"                           # 固定版本；升级前先在研究机上跑 tests/test_fwresearch.py
while [[ $# -gt 0 ]]; do
  case "$1" in
    --openclaw-user) OC_USER="$2"; shift 2 ;;
    --prefix) PREFIX="${2%/}"; shift 2 ;;
    --with-vibe-trading) WITH_VT=1; shift ;;
    --with-news) WITH_NEWS=1; shift ;;
    --with-blockbeats) WITH_BB=1; WITH_NEWS=1; shift ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
R="$PREFIX"                                   # 目标根目录（正式部署为空）
say() { printf '\n== %s\n' "$*"; }
fake() { [[ -n "$PREFIX" ]]; }

if ! fake && [[ $EUID -ne 0 ]]; then echo "请用 root 运行（sudo bash deploy/install.sh）"; exit 1; fi
if ! fake && ! id "$OC_USER" >/dev/null 2>&1; then
  echo "找不到 OpenClaw 用户 $OC_USER：请先按 OpenClaw 官方方式安装，或用 --openclaw-user 指定"; exit 1
fi
OC_HOME="$R/home/$OC_USER"
fake || OC_HOME="$(getent passwd "$OC_USER" | cut -d: -f6)"

rand() { python3 -c 'import secrets;print(secrets.token_urlsafe(36))'; }
own() { fake || chown "$@"; }

say "1/8 系统用户 s03trader"
if ! fake; then id s03trader >/dev/null 2>&1 || useradd --system --home /var/lib/s03-ft --shell /usr/sbin/nologin s03trader; fi

say "2/8 代码与 Python 环境（/opt/s03）"
install -d -m 0755 "$R/opt/s03"
rm -rf "$R/opt/s03/ft-s03.new" && mkdir -p "$R/opt/s03/ft-s03.new"
( cd "$SRC" && tar --exclude='./user_data/data' --exclude='./user_data/backtest_results' --exclude='./user_data/logs' \
    --exclude='./monitor/var' --exclude='./research/results' --exclude='*/__pycache__' --exclude='./.git' \
    --exclude='*.egg-info' -cf - . ) | tar -xf - -C "$R/opt/s03/ft-s03.new"
rm -rf "$R/opt/s03/ft-s03" && mv "$R/opt/s03/ft-s03.new" "$R/opt/s03/ft-s03"
if ! fake; then
  [[ -x /opt/s03/venv/bin/python ]] || python3 -m venv /opt/s03/venv
  /opt/s03/venv/bin/pip install -q --upgrade pip
  /opt/s03/venv/bin/pip install -q -e "/opt/s03/ft-s03[trade]"
  ln -sf /opt/s03/venv/bin/s03ft /usr/local/bin/s03ft
  if [[ $WITH_VT == 1 ]]; then
    [[ -x /opt/s03/vt-venv/bin/python ]] || python3 -m venv /opt/s03/vt-venv
    /opt/s03/vt-venv/bin/pip install -q --upgrade pip
    /opt/s03/vt-venv/bin/pip install -q "vibe-trading-ai==$VT_VERSION"
  fi
  if [[ $WITH_BB == 1 ]]; then
    command -v npm >/dev/null || { echo "需要 npm（OpenClaw 依赖 Node，通常已安装）"; exit 1; }
    bash "$SRC/deploy/bin/s03-install-blockbeats-mcp" /opt/s03/bb-mcp
  fi
  chown -R root:root /opt/s03 && chmod -R go-w /opt/s03
fi

say "3/8 配置与密钥（/etc/s03；已存在的文件一律不覆盖）"
E="$R/etc/s03"; install -d -m 0755 "$E"
newfile() { [[ ! -e "$1" ]]; }
if newfile "$E/ft-config.json"; then install -m 0644 "$SRC/user_data/config.json" "$E/ft-config.json"; fi
if newfile "$E/ft-api.json"; then
  python3 - "$SRC/deploy/ft-api.json" "$E/ft-api.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8")); d.pop("_说明", None)
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
  chmod 0644 "$E/ft-api.json"
fi
if newfile "$E/monitor.toml"; then install -m 0644 "$SRC/monitor/monitor.example.toml" "$E/monitor.toml"; fi
if newfile "$E/ft-trader.env"; then
  APIU="s03api"; APIP="$(rand)"
  umask 077
  cat > "$E/ft-trader.env" <<EOF
# 由 install.sh 生成（root 0600）。请填入 Binance API Key（只勾选读取与合约交易，不开提现，限制服务器 IP）。
FREQTRADE__EXCHANGE__KEY=
FREQTRADE__EXCHANGE__SECRET=
FREQTRADE__API_SERVER__USERNAME=$APIU
FREQTRADE__API_SERVER__PASSWORD=$APIP
FREQTRADE__API_SERVER__JWT_SECRET_KEY=$(rand)
FREQTRADE__API_SERVER__WS_TOKEN=$(rand)
EOF
  cat > "$E/ft-monitor.env" <<EOF
# 由 install.sh 生成（root 0600）：与 ft-trader.env 中的 API 账号一致
FT_API_USER=$APIU
FT_API_PASS=$APIP
EOF
  umask 022
fi
if newfile "$E/openclaw-hooks.env"; then
  ( umask 077; printf '# 由 install.sh 生成：把同一个值填进 OpenClaw 配置的 hooks.token\nOPENCLAW_HOOKS_TOKEN=%s\n' "$(rand)" > "$E/openclaw-hooks.env" )
fi
if newfile "$E/openclaw-gate.env"; then
  GT="$(rand)"
  ( umask 077; printf 'OPENCLAW_GATE_TOKEN=%s\n' "$GT" > "$E/openclaw-gate.env" )
  install -d -m 0700 "$E/openclaw"
  ( umask 077; printf '%s\n' "$GT" > "$E/openclaw/gate.token" )
  own "$OC_USER:$OC_USER" "$E/openclaw" "$E/openclaw/gate.token"; chmod 0400 "$E/openclaw/gate.token"
fi
if newfile "$E/events.env"; then install -m 0644 "$SRC/deploy/env/events.env.example" "$E/events.env"; fi
if newfile "$E/heartbeat.env"; then install -m 0600 "$SRC/deploy/env/heartbeat.env.example" "$E/heartbeat.env"; fi
if newfile "$E/vibe-trading.env"; then install -m 0640 "$SRC/deploy/env/vibe-trading.env.example" "$E/vibe-trading.env"; fi
if newfile "$E/blockbeats.env"; then install -m 0640 "$SRC/deploy/env/blockbeats.env.example" "$E/blockbeats.env"; fi
chmod 0600 "$E"/*.env 2>/dev/null || true; chmod 0644 "$E/events.env"
chmod 0640 "$E/vibe-trading.env" "$E/blockbeats.env"; own "root:$OC_USER" "$E/vibe-trading.env" "$E/blockbeats.env"   # Gateway（openclaw 用户）启动 MCP 时要读；s03-news 由 systemd 以 root 读入

say "4/8 工作目录（复盘收件箱、研究副本）"
install -d -m 0750 "$R/srv/s03-review" "$R/srv/s03-research" "$R/srv/s03-research/vt-runs" "$R/srv/s03-research/research/lab"
install -d -m 2750 "$R/srv/s03-review/inbox"
own "$OC_USER:$OC_USER" "$R/srv/s03-review" "$R/srv/s03-research"
own "s03trader:$OC_USER" "$R/srv/s03-review/inbox"
for d in user_data tests monitor research examples s03ft fwresearch pyproject.toml docs; do
  [[ -e "$R/srv/s03-research/$d" ]] || cp -r "$SRC/$d" "$R/srv/s03-research/"
done
rm -rf "$R/srv/s03-research/user_data/data" "$R/srv/s03-research/user_data/config-private.example.json"
fake || chown -R "$OC_USER:$OC_USER" /srv/s03-research

say "5/8 小工具、systemd 服务、sudoers"
install -D -m 0755 "$SRC/deploy/bin/s03-gate"       "$R/usr/local/bin/s03-gate"
for b in s03-hook s03-ft-export s03-heartbeat s03-vt-mcp s03-bb-mcp s03-install-blockbeats-mcp; do install -D -m 0755 "$SRC/deploy/bin/$b" "$R/usr/local/lib/s03/$b"; done
install -d "$R/etc/systemd/system" && install -m 0644 "$SRC"/deploy/systemd/* "$R/etc/systemd/system/"
install -d "$R/etc/sudoers.d"
if fake; then
  install -m 0440 "$SRC/deploy/sudoers/s03-openclaw" "$R/etc/sudoers.d/s03-openclaw"
else
  sed "s/^openclaw ALL=/$OC_USER ALL=/; s/^Defaults:openclaw /Defaults:$OC_USER /" "$SRC/deploy/sudoers/s03-openclaw" > /tmp/s03-sudoers.$$
  visudo -cf /tmp/s03-sudoers.$$ && install -m 0440 -o root -g root /tmp/s03-sudoers.$$ /etc/sudoers.d/s03-openclaw
  rm -f /tmp/s03-sudoers.$$
  systemctl daemon-reload
fi

say "6/8 OpenClaw 工作区与技能"
W="$SRC/deploy/openclaw/workspaces"
for a in s03-ops s03-analyst; do
  install -d -m 0750 "$OC_HOME/.openclaw/workspace-$a"
  cp -r "$W/$a/." "$OC_HOME/.openclaw/workspace-$a/"
  fake || chown -R "$OC_USER:$OC_USER" "$OC_HOME/.openclaw/workspace-$a"
done
cp -r "$W/s03-research/." "$R/srv/s03-research/"
cp -r "$W/s03-review/." "$R/srv/s03-review/"
fake || chown -R "$OC_USER:$OC_USER" /srv/s03-research /srv/s03-review/AGENTS.md /srv/s03-review/skills
install -d -m 0750 "$R/etc/s03/openclaw-templates"
install -m 0640 "$SRC/deploy/openclaw/openclaw.s03.json5" "$SRC/deploy/openclaw/exec-approvals.s03.json" "$R/etc/s03/openclaw-templates/"

say "7/8 启用服务与定时器（不启动 freqtrade）"
if ! fake; then
  systemctl enable s03-freqtrade.service s03-ft-monitor.service s03-ft-gate.service \
    s03-events.timer s03-heartbeat.timer s03-wake-daily.timer s03-wake-weekly.timer
  systemctl start s03-events.service || echo "事件日历首次生成失败：请先在 /etc/s03/events.env 填联系邮箱，再 systemctl start s03-events"
  if [[ $WITH_NEWS == 1 ]]; then
    systemctl enable s03-news.timer s03-news-data.timer
    echo "新闻守卫：填好 /etc/s03/blockbeats.env 后运行 systemctl start s03-news.timer s03-news-data.timer；s03ft status 的 news_guard 应为 ok"
  fi
fi

say "8/8 自检"
if ! fake; then sudo -u "$OC_USER" /usr/local/bin/s03ft doctor || true; fi
if ! fake && [[ $WITH_VT == 1 ]]; then
  # Vibe-Trading 自检：版本、因子库桥接、MCP 握手、白名单（26 个工具）、拒绝交易工具、密钥过滤
  sudo -u "$OC_USER" env VIBE_TRADING_HOME=/srv/s03-research/.vibe-trading /usr/local/bin/s03ft lab vt-check || \
    echo "Vibe-Trading 自检未全部通过：看上面 JSON 里 ok=false 的项，说明见 docs/RESEARCH_LAB.md"
fi

cat <<EOF

部署文件已就位。还需要你本人完成（按顺序）：
  1. 编辑 /etc/s03/ft-trader.env 填入 Binance API Key；/etc/s03/events.env 填联系邮箱；/etc/s03/heartbeat.env 填心跳地址。
  2. 合并 /etc/s03/openclaw-templates/openclaw.s03.json5 到 OpenClaw 配置（hooks.token 用 /etc/s03/openclaw-hooks.env 里的值），
     导入 exec-approvals.s03.json，然后 openclaw doctor。
  3. 首次启动由你执行：systemctl start s03-ft-gate s03-ft-monitor s03-freqtrade，再以 $OC_USER 用户运行 s03ft doctor。
  4. dry_run 默认为 true；改为 false 属于 G9，只能由你在 /etc/s03/ft-config.json 中修改并重启。
  5. （可选）研究前端找想法：用 --with-vibe-trading 重跑本脚本，在 /etc/s03/vibe-trading.env 填大模型配置，
     再把 OpenClaw 配置里 mcp.servers["vibe-trading"].enabled 改为 true。说明见 docs/RESEARCH_LAB.md。
  6. （可选）新闻事件守卫（Odaily + BlockBeats 两家同时）：用 --with-news 重跑本脚本（--with-blockbeats 另装研究 MCP），
     在 /etc/s03/blockbeats.env 填 BlockBeats Key 后自检：
     sudo bash -c 'set -a; . /etc/s03/blockbeats.env; cd /opt/s03/ft-s03; /opt/s03/venv/bin/python -m fwresearch news check'
     两个来源都 ok: true 后 systemctl start s03-news.timer s03-news-data.timer；s03ft status 的 news_guard 应为 ok
     （degraded 表示有一家这一轮失败，封禁照常但要查原因）。
     研究 agent 的 MCP：把 OpenClaw 配置里 mcp.servers["blockbeats"].enabled 改为 true。说明见 docs/RESEARCH_LAB.md。
EOF
