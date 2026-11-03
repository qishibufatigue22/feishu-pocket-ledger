"""Safe subprocess adapter and response decoding for the official lark-cli."""

import json
import os
import subprocess


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class CLIError(RuntimeError):
    pass


class CLI:
    def __init__(self, profile=None):
        self.prefix = ["lark-cli"] + (["--profile", profile] if profile else [])

    def call(self, *args, identity="user"):
        env = dict(
            os.environ,
            LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1",
            LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1",
        )
        result = subprocess.run(
            self.prefix + list(args) + ["--as", identity],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        try:
            output = json.loads(
                result.stdout if result.returncode == 0 else result.stderr
            )
        except ValueError:
            raise CLIError(
                "CLI 未返回有效 JSON；请运行 lark-cli doctor 检查配置。"
            ) from None
        if result.returncode or output.get("ok") is not True:
            error = output.get("error", {})
            # Do not log command arguments, credentials, or private expense data.
            raise CLIError(
                f"飞书调用失败：{error.get('subtype', 'unknown')}；{error.get('hint', '请检查 CLI 权限与登录状态。')}"
            )
        return output.get("data", output)


def objects(value):
    """Walk envelopes without depending on a wrapper's optional nesting."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def find_id(data, *keys):
    for item in objects(data):
        for key in keys:
            if isinstance(item.get(key), str) and item[key]:
                return item[key]
    raise CLIError("飞书返回缺少资源 ID；需要核对当前 CLI 返回格式。")


def decode_records(data):
    if isinstance(data, dict) and all(
        k in data for k in ("data", "fields", "record_id_list")
    ):
        rows, names, ids = data["data"], data["fields"], data["record_id_list"]
        if len(rows) != len(ids) or any(len(r) != len(names) for r in rows):
            raise CLIError("CLI 记录矩阵维度不一致，已停止写入。")
        return [dict(zip(names, row), record_id=rid) for row, rid in zip(rows, ids)]
    if isinstance(data, list) and all(
        isinstance(r, dict) and "record_id" in r for r in data
    ):
        return [{**r.get("fields", r), "record_id": r["record_id"]} for r in data]
    for item in objects(data):
        for key in ("records", "items"):
            rows = item.get(key)
            if isinstance(rows, list) and (
                not rows or all(isinstance(r, dict) and "record_id" in r for r in rows)
            ):
                return [
                    {**r.get("fields", r), "record_id": r["record_id"]} for r in rows
                ]
        rows, columns = item.get("rows"), item.get("columns")
        if isinstance(rows, list) and isinstance(columns, list):
            names = [
                c if isinstance(c, str) else c.get("name", c.get("field_name"))
                for c in columns
            ]
            result = [
                dict(zip(names, row)) if isinstance(row, list) else row for row in rows
            ]
            if all("record_id" in r for r in result):
                return result
    raise CLIError("无法识别 CLI 的记录格式，已停止写入以避免重复记账。")
