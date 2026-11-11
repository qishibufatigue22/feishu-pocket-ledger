"""Install this checkout as a per-user macOS background service."""

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

LABEL = "com.feishu-pocket-ledger.robot"


def install():
    if sys.platform != "darwin":
        raise SystemExit("此安装脚本仅支持 macOS。")
    root = Path(__file__).resolve().parent.parent
    if not (root / "config.json").exists():
        raise SystemExit("请先配置项目目录中的 config.json。")
    cli = shutil.which("lark-cli")
    if not cli:
        raise SystemExit("请先安装并配置 lark-cli。")
    # launchd does not inherit an interactive app's Documents permission.
    # Keep an application-owned runtime copy outside protected user folders.
    runtime = Path.home() / "Library" / "Application Support" / "feishu-pocket-ledger"
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    domain = f"gui/{os.getuid()}"
    service = domain + "/" + LABEL
    subprocess.run(["launchctl", "bootout", service], capture_output=True)
    # The old service may remain registered briefly after bootout returns.
    deadline = time.monotonic() + 10
    while subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0:
        if time.monotonic() >= deadline:
            raise SystemExit("旧服务尚未卸载，请稍后重试。")
        time.sleep(0.25)
    for name in ("robot.py", "feishu_cli.py", "dashboard_setup.py", "natural_intent.py"):
        shutil.copy2(root / name, runtime / name)
    for package in ("agent", "tools", "knowledge", "storage", "adapters"):
        shutil.copytree(root / package, runtime / package, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config = runtime / "config.json"
    if not config.exists():
        shutil.copy2(root / "config.json", config)
    config.chmod(0o600)
    logs = runtime / ".logs"
    logs.mkdir(exist_ok=True)
    target = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    target.parent.mkdir(parents=True, exist_ok=True)
    path = os.pathsep.join(dict.fromkeys([
        str(Path(cli).parent), str(Path(sys.executable).parent),
        os.environ.get("PATH", "/usr/bin:/bin"),
    ]))
    settings = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-u", str(runtime / "robot.py")],
        "WorkingDirectory": str(runtime),
        "EnvironmentVariables": {"PATH": path, "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "robot.stdout.log"),
        "StandardErrorPath": str(logs / "robot.stderr.log"),
    }
    target.write_bytes(plistlib.dumps(settings))
    target.chmod(0o600)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print("已安装并启动：" + LABEL)
    print("状态：launchctl print " + domain + "/" + LABEL)
    print("日志：" + str(logs / "robot.stderr.log"))
    print("运行配置：" + str(config) + "（更新源码后重新运行本安装脚本；保留已有运行配置）")


if __name__ == "__main__":
    install()
