import calendar
import datetime as dt
import decimal
import html
import json
import os
import re
import time
from pathlib import Path
from feishu_cli import CLI, CLIError, dumps, objects, find_id, decode_records
from knowledge.categories import CATEGORIES

def fields(names):
    return [{"name": n, "type": "text"} for n in names]

DETAIL_SCHEMA = fields(
    ["消息ID", "日期", "项目", "大类", "分类", "状态", "发送者", "分类回复ID"]
)

DETAIL_SCHEMA += [
    {
        "name": "金额",
        "type": "number",
        "style": {"type": "currency", "currency_code": "CNY", "precision": 2},
    }
]

MONTH_SCHEMA = fields(["日期"])

MONTH_SCHEMA += [
    {
        "name": f"{group}·{c}",
        "type": "number",
        "style": {"type": "currency", "currency_code": "CNY", "precision": 2},
    }
    for c, group in CATEGORIES.items()
]

MONTH_SCHEMA += [
    {
        "name": "当日合计",
        "type": "number",
        "style": {"type": "currency", "currency_code": "CNY", "precision": 2},
    }
]


class FeishuStore:
    def __init__(self, cli, config, config_path):
        self.cli, self.config, self.path = cli, config, config_path
        self.confirmed_details = {}

    def save_config(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(dumps(self.config), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def is_completed(self, message_id):
        return message_id in self.config.get("completed_messages", [])

    def mark_completed(self, message_id):
        if not self.is_completed(message_id):
            self.config.setdefault("completed_messages", []).append(message_id)
            self.save_config()

    def year(self, year):
        key, name = str(year), f"{year}年账本"
        if key in self.config["years"]:
            return self.config["years"][key]
        tokens, page = set(), None
        while True:
            args = [
                "drive",
                "+search",
                "--query",
                name,
                "--doc-types",
                "bitable",
                "--only-title",
                "--mine",
            ]
            if page:
                args += ["--page-token", page]
            result = self.cli.call(*args)
            if not isinstance(result.get("results"), list):
                raise CLIError("无法识别账本搜索结果，停止创建以避免重复。")
            for match in result["results"]:
                title = html.unescape(
                    re.sub(r"</?h>", "", match.get("title_highlighted", ""))
                )
                if title == name:
                    tokens.add(match["result_meta"]["token"])
            if not result.get("has_more"):
                break
            page = result.get("page_token")
            if not page:
                raise CLIError("账本搜索分页缺少下一页标记。")
        if len(tokens) > 1:
            raise CLIError(
                f"发现多个同名{name}，请在 config.json 的 years 中指定正确的 token。"
            )
        if tokens:
            token = tokens.pop()
        else:
            result = self.cli.call(
                "base",
                "+base-create",
                "--name",
                name,
                "--time-zone",
                "Asia/Shanghai",
                "--table-name",
                "消费明细",
                "--fields",
                dumps(DETAIL_SCHEMA),
            )
            token = find_id(result, "base_token", "app_token", "token")
        self.config["years"][key] = token
        self.save_config()
        self.table(token, "消费明细", DETAIL_SCHEMA)
        return token

    def table(self, token, name, schema):
        result = self.cli.call("base", "+table-list", "--base-token", token)
        matches = {
            r.get("table_id", r.get("id"))
            for r in objects(result)
            if r.get("name") == name and r.get("table_id", r.get("id"))
        }
        if len(matches) > 1:
            raise CLIError(f"存在多个同名子表{name}，请先整理表名。")
        if matches:
            return matches.pop()
        result = self.cli.call(
            "base",
            "+table-create",
            "--base-token",
            token,
            "--name",
            name,
            "--fields",
            dumps(schema),
        )
        return find_id(result, "table_id", "id")

    def records(self, token, table, conditions=None):
        result, offset = [], 0
        while True:
            args = [
                "base",
                "+record-list",
                "--base-token",
                token,
                "--table-id",
                table,
                "--format",
                "json",
                "--limit",
                "200",
                "--offset",
                str(offset),
            ]
            if conditions:
                args += [
                    "--filter-json",
                    dumps({"logic": "and", "conditions": conditions}),
                ]
            data = self.cli.call(*args)
            rows = decode_records(data)
            result.extend(rows)
            more = any(o.get("has_more") is True for o in objects(data))
            if not more and len(rows) < 200:
                return result
            if not rows:
                raise CLIError("飞书分页未向前推进，停止读取。")
            offset += len(rows)

    def write(self, token, table, values, record_id=None):
        args = [
            "base",
            "+record-upsert",
            "--base-token",
            token,
            "--table-id",
            table,
            "--json",
            dumps(values),
        ]
        if record_id:
            args += ["--record-id", record_id]
        data = self.cli.call(*args)
        if any(o.get("ignored_fields") for o in objects(data)):
            raise CLIError("部分字段被飞书忽略，请检查表格字段类型。")
        # Base writes propagate asynchronously. Wait for the actual values before
        # deriving summaries or telling the user that posting has finished.
        key = "消息ID" if table == "消费明细" else "日期"
        if record_id or key in values:
            deadline = time.monotonic() + 25
            while True:
                rows = self.records(token, table)
                matches = [
                    r
                    for r in rows
                    if (
                        r["record_id"] == record_id
                        if record_id
                        else r.get(key) == values[key]
                    )
                ]
                if len(matches) == 1 and all(
                    (
                        matches[0].get(k) in (None, "")
                        if v in (None, "")
                        else matches[0].get(k) == v
                    )
                    for k, v in values.items()
                ):
                    if table == "消费明细":
                        self.confirmed_details[token] = rows
                    break
                if time.monotonic() >= deadline:
                    raise CLIError(
                        "飞书写入已提交，但尚未确认同步完成；请核对明细，暂勿重复发送消费。"
                    )
                time.sleep(1)
        return data

    def ensure_month(self, date):
        token = self.year(date.year)
        table = self.table(token, f"{date.month}月账单", MONTH_SCHEMA)
        existing = {r["日期"] for r in self.records(token, table)}
        # Recover an interrupted initialization without adding existing dates twice.
        missing = []
        for day in range(1, calendar.monthrange(date.year, date.month)[1] + 1):
            value = date.replace(day=day).isoformat()
            if value not in existing:
                missing.append({"日期": value})
        if missing:
            self.cli.call(
                "base",
                "+record-batch-create",
                "--base-token",
                token,
                "--table-id",
                table,
                "--json",
                dumps({"create_records": missing}),
            )
        if "总计" not in existing:
            self.sync_month(token, table, date)
        return token, table

    def link(self, token, table):
        data = self.cli.call("base", "+base-get", "--base-token", token)
        return data["base"]["url"] + ("?table=" + table if table else "")

    def get_bill_link(self, year, month):
        token = self.config["years"].get(str(year))
        if not token:
            return None
        if month is None:
            return self.link(token, None)
        tables = self.cli.call("base", "+table-list", "--base-token", token)["tables"]
        matches = [t for t in tables if t["name"] == f"{month}月账单"]
        if len(matches) > 1:
            raise CLIError("发现同名月份表，请先明确要打开的子表。")
        return self.link(token, matches[0]["id"]) if matches else None

    def ensure_charts(self, year):
        from dashboard_setup import setup

        token = self.config["years"].get(str(year))
        if not token:
            raise CLIError(f"尚未创建{year}年账本，请先新建月份账单。")
        try:
            dashboard = setup(self.cli, token, year)
        except RuntimeError as exc:
            raise CLIError(str(exc)) from None
        self.config.setdefault("dashboards", {})[str(year)] = dashboard
        self.save_config()
        return self.link(token, dashboard)

    def sync_day(self, token, table, date):
        snapshot = self.confirmed_details.pop(token, None)
        if snapshot is None:
            snapshot = self.records(token, "消费明细")
        records = [
            r
            for r in snapshot
            if r.get("日期") == date.isoformat() and r.get("状态") == "已入账"
        ]
        totals = {c: 0 for c in CATEGORIES}
        for r in records:
            if r.get("分类") not in totals:
                raise CLIError("明细中有未知分类，请先检查消费明细。")
            totals[r["分类"]] += int(decimal.Decimal(str(r["金额"])) * 100)
        values = {
            f"{CATEGORIES[c]}·{c}": cents / 100 if cents else None
            for c, cents in totals.items()
        }
        values["当日合计"] = sum(totals.values()) / 100 or None
        rows = self.records(token, table, [["日期", "==", date.isoformat()]])
        if len(rows) != 1:
            raise CLIError("每日汇总日期不唯一，请先检查月份表。")
        self.write(token, table, values, rows[0]["record_id"])
        self.sync_month(token, table, date, snapshot)
        return records

    def sync_month(self, token, table, date, snapshot=None):
        """Rebuild the footer from posted facts, excluding audit/pending rows."""
        prefix = date.strftime("%Y-%m-")
        totals = {c: 0 for c in CATEGORIES}
        if snapshot is None:
            snapshot = self.records(token, "消费明细", [["状态", "==", "已入账"]])
        for row in snapshot:
            if row.get("状态") != "已入账" or not row.get("日期", "").startswith(
                prefix
            ):
                continue
            category = row.get("分类")
            if category not in totals:
                raise CLIError("明细中有未知分类，月总计未更新，请检查消费明细。")
            totals[category] += int(decimal.Decimal(str(row["金额"])) * 100)
        values = {
            f"{CATEGORIES[c]}·{c}": cents / 100 if cents else None
            for c, cents in totals.items()
        }
        values.update({"日期": "总计", "当日合计": sum(totals.values()) / 100 or None})
        rows = self.records(token, table, [["日期", "==", "总计"]])
        if len(rows) > 1:
            raise CLIError("月份表中有多个总计行，请先检查表格。")
        self.write(token, table, values, rows[0]["record_id"] if rows else None)

    def pending(self, sender):
        result = []
        for token in self.config["years"].values():
            for row in self.records(
                token, "消费明细", [["状态", "==", "待分类"], ["发送者", "==", sender]]
            ):
                result.append((token, row))
        return result

    def processed(self, message_id):
        for token in self.config["years"].values():
            for column in ("消息ID", "分类回复ID"):
                rows = self.records(token, "消费明细", [[column, "==", message_id]])
                if rows:
                    return token, rows[0]
        return None

    def correction_candidates(self, date, sender):
        token = self.config["years"].get(str(date.year))
        if not token:
            return None, []
        return token, self.records(
            token,
            "消费明细",
            [
                ["日期", "==", date.isoformat()],
                ["状态", "==", "已入账"],
                ["发送者", "==", sender],
            ],
        )
