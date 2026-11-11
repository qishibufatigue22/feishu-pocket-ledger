"""Conversation state is stored separately from annual expense ledgers."""
import copy
import json
import time
from feishu_cli import CLIError, dumps, find_id


class MemoryState:
    """In-memory backend for isolated tests."""
    def __init__(self):
        self.data = {"task": None, "rules": {}}

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, data):
        self.data = copy.deepcopy(data)


class FeishuState:
    def __init__(self, store, owner):
        self.store, self.owner = store, owner

    def load(self):
        token = self.store.config.get("agent_state_base")
        if not token:
            return {"task": None, "rules": {}}
        rows = self.store.records(token, "对话状态", [["用户", "==", self.owner]])
        if len(rows) > 1:
            raise CLIError("存在重复的Agent状态行，请先核对配置表。")
        return json.loads(rows[0]["状态数据"]) if rows else {"task": None, "rules": {}}

    def save(self, data):
        token = self.store.config.get("agent_state_base")
        if not token:
            result = self.store.cli.call(
                "base", "+base-create", "--name", "记账机器人配置",
                "--table-name", "对话状态", "--fields",
                dumps([{"name": "用户", "type": "text"}, {"name": "状态数据", "type": "text"}]),
                "--time-zone", "Asia/Shanghai",
            )
            token = find_id(result, "base_token", "app_token", "token")
            self.store.config["agent_state_base"] = token
            self.store.save_config()
        rows = self.store.records(token, "对话状态", [["用户", "==", self.owner]])
        if len(rows) > 1:
            raise CLIError("存在重复的Agent状态行，停止更新。")
        payload = dumps(data)
        self.store.write(token, "对话状态", {"用户": self.owner, "状态数据": payload},
                         rows[0]["record_id"] if rows else None)
        deadline = time.monotonic() + 25
        while True:
            found = self.store.records(token, "对话状态", [["用户", "==", self.owner]])
            if len(found) == 1 and found[0].get("状态数据") == payload:
                return
            if time.monotonic() > deadline:
                raise CLIError("对话状态尚未同步完成，请稍后重试。")
            time.sleep(1)
