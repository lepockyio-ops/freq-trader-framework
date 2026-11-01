"""一键部署脚本测试：装到假根目录，检查文件布局、密钥权限、重复运行不覆盖密钥。"""
import hashlib
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                    reason="Linux 部署测试需要 POSIX 文件权限与可运行的 bash；Windows 应在 Linux CI 中运行此项")
def test_install_fake_root(tmp_path):
    fr = tmp_path / "root"
    run = lambda: subprocess.run(["bash", str(ROOT / "deploy" / "install.sh"), "--prefix", str(fr)],  # noqa: E731
                                 capture_output=True, text=True)
    r = run()
    assert r.returncode == 0, r.stderr[-2000:]
    e = fr / "etc" / "s03"
    for f in ("ft-trader.env", "ft-monitor.env", "openclaw-hooks.env", "openclaw-gate.env"):
        assert stat.S_IMODE((e / f).stat().st_mode) == 0o600, f
    assert stat.S_IMODE((e / "openclaw" / "gate.token").stat().st_mode) == 0o400
    assert "_说明" not in json.loads((e / "ft-api.json").read_text("utf-8"))
    assert json.loads((e / "ft-config.json").read_text("utf-8"))["dry_run"] is True
    assert (fr / "home/openclaw/.openclaw/workspace-s03-ops/skills/s03ft-ops/SKILL.md").exists()
    assert (fr / "srv/s03-research/skills/s03ft-research/SKILL.md").exists()
    assert (fr / "srv/s03-review/skills/s03ft-review/SKILL.md").exists()
    assert not (fr / "srv/s03-research/user_data/config-private.example.json").exists()
    h = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in e.glob("*.env")}
    assert run().returncode == 0
    assert h == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in e.glob("*.env")}   # 不覆盖密钥
    env = (e / "ft-trader.env").read_text("utf-8")
    assert "FREQTRADE__EXCHANGE__KEY=\n" in env                       # 交易所 Key 留空，由人填写
