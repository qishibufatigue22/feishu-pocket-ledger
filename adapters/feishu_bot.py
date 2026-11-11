import os
import fcntl
import json
import logging
import signal
import subprocess
import threading
from pathlib import Path
from feishu_cli import CLI, CLIError
from storage.feishu import FeishuStore
from storage.state import FeishuState
from agent.orchestrator import Robot

def run():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    # A single worker keeps read/compute/write summaries serialized.
    lock = (root / ".robot.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("机器人已运行，请勿重复启动。")
    path = root / "config.json"
    if not path.exists():
        raise SystemExit(
            "请复制 config.example.json 为 config.json，并填写 owner_open_id。"
        )
    config = json.loads(path.read_text(encoding="utf-8"))
    owner = config.get("owner_open_id", "")
    if not owner.startswith("ou_"):
        raise SystemExit("请在 config.json 填写你的 owner_open_id（ou_ 开头）。")
    config.setdefault("years", {})
    cli = CLI(config.get("profile"))
    store = FeishuStore(cli, config, path)
    robot = Robot(store, owner, state=FeishuState(store, owner))
    stop = threading.Event()
    child = None

    def shutdown(*_):
        stop.set()
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    while not stop.is_set():
        # Keep stdin open: lark-cli treats EOF as a graceful shutdown request.
        child = subprocess.Popen(
            cli.prefix + ["event", "consume", "im.message.receive_v1", "--as", "bot"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = threading.Event()

        def diagnostics(process, ready_event):
            for line in process.stderr:
                if "[event] ready event_key=" in line:
                    ready_event.set()
                    logging.info("飞书消息监听已就绪")
                elif "WARN" in line or '"ok":false' in line or '"ok": false' in line:
                    logging.warning(
                        "飞书事件监听返回警告或错误，请检查 CLI 配置及权限。"
                    )

        threading.Thread(target=diagnostics, args=(child, ready), daemon=True).start()
        for line in child.stdout:
            try:
                event = json.loads(line)
                if not ready.is_set():
                    if not ready.wait(10):
                        raise CLIError("飞书事件流未发出 ready 标记。")
                try:
                    reply = robot.handle(event)
                    completed = True
                except ValueError as exc:
                    reply = str(exc)
                    completed = True
                except CLIError as exc:
                    completed = False
                    logging.error("账单写入或汇总未完成，请检查飞书权限及表格。")
                    reply = (
                        str(exc) + "\n本次处理未全部完成，请检查消费明细后重试原消息。"
                    )
                if reply:
                    cli.call(
                        "im",
                        "+messages-reply",
                        "--message-id",
                        event["message_id"],
                        "--text",
                        reply,
                        "--idempotency-key",
                        event["message_id"],
                        identity="bot",
                    )
                    if completed:
                        robot.store.mark_completed(event["message_id"])
            except (CLIError, ValueError, KeyError, subprocess.TimeoutExpired):
                logging.exception("消息处理失败")
        code = child.wait()
        if not stop.is_set():
            logging.warning("事件监听退出（%s），10秒后重连", code)
            stop.wait(10)
