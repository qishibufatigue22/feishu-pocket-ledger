"""Personal Feishu expense bot. All expense records live in Feishu Base."""

import calendar
import datetime as dt
import decimal
import fcntl
import html
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

from feishu_cli import CLI, CLIError, decode_records, dumps, find_id, objects

TZ = ZoneInfo("Asia/Shanghai")
CATEGORIES = {
    "房租/物业水电": "固定费",
    "基金": "固定费",
    "伙食费": "流动费",
    "日用品": "流动费",
    "零花钱": "流动费",
    "交通": "流动费",
    "维修": "特别备用金",
    "旅行": "特别备用金",
    "生日": "特别备用金",
}
WORDS = {
    "房租/物业水电": ("房租", "物业", "水费", "电费", "燃气", "租金", "水电费"),
    "基金": ("基金", "定投"),
    "伙食费": (
        "早餐",
        "午饭",
        "晚饭",
        "早饭",
        "午餐",
        "晚餐",
        "买菜",
        "外卖",
        "餐厅",
        "饭店",
        "食堂",
        "快餐",
        "面馆",
        "火锅",
        "烧烤",
        "麻辣烫",
        "盒饭",
    ),
    "日用品": (
        "卫生纸",
        "卫生巾",
        "洗洁精",
        "清洁",
        "洗衣液",
        "垃圾袋",
        "牙膏",
        "抽纸",
        "卷纸",
        "牙刷",
        "洗衣粉",
        "洗手液",
        "洁厕",
        "拖把",
        "乳胶手套",
        "保鲜袋",
        "保鲜膜",
    ),
    "零花钱": (
        "衣服",
        "护肤",
        "零食",
        "爱好",
        "口红",
        "面膜",
        "化妆品",
        "洗面奶",
        "防晒霜",
        "饼干",
        "薯片",
        "糖果",
        "巧克力",
        "icloud",
        "gpt",
    ),
    "交通": (
        "地铁",
        "公交",
        "打车",
        "出租车",
        "滴滴",
        "停车",
        "共享单车",
        "高铁",
        "动车",
        "火车",
        "车票",
        "机票",
        "飞机票",
        "航班",
        "网约车",
        "顺风车",
        "哈啰单车",
        "美团单车",
        "青桔单车",
        "骑行费",
        "轮渡",
        "船票",
        "过路费",
        "高速费",
        "过桥费",
        "加油费",
        "充电费",
    ),
    "维修": ("维修", "修理"),
    "旅行": ("旅行", "旅游"),
    "生日": ("生日",),
}
ALIASES = {"房租": "房租/物业水电", "水电": "房租/物业水电", "伙食": "伙食费"}
HELP = "新建2026年11月账单\n午饭20\n11月3日 卫生纸15.9\n2026-10-03 地铁3\n盒马6.9（不确定分类时会询问）\n分类可回复名称或1—9；回复取消可取消待确认记录。"


def extract_category(text):
    """Separate an explicit trailing category before parsing the amount."""
    match = re.search(
        r"\s*(?:（([^（）]+)）|\(([^()]+)\)|[，,；;]?\s*(?:属于|归类为|分类为|归为)\s*([^，,；;（）()]+))\s*[。！!]?\s*$",
        text,
    )
    if not match:
        return text, None
    category = next(group for group in match.groups() if group).strip()
    category = re.sub(r"^(?:固定费|流动费|特别备用金)\s*[·/：:]\s*", "", category)
    category = ALIASES.get(category, category)
    if category not in CATEGORIES:
        raise ValueError(
            "没有识别出你指定的分类，请使用日用品、伙食费、零花钱、交通等账本中的分类名称。"
        )
    return text[: match.start()].rstrip(" ，,；;"), category


def parse_correction(text, today):
    """Correction intent must never fall through to expense creation."""
    if not re.search(r"不对|记错|写错|更正|改成|改为|修改|应该是|应为|实际上是", text):
        return None
    cleaned = re.sub(
        r"^(?:不对|记错了?|写错了?|更正一下|更正|修改一下|修改)[，,：:\s]*",
        "",
        text.strip(),
    )
    cleaned = re.sub(r"^(?:把|将)", "", cleaned)
    old = re.search(r"(\d+(?:\.\d{1,2})?)\s*元?\s*(?:改成|改为|修改为)", cleaned)
    old_cents = int(decimal.Decimal(old[1]) * 100) if old else None
    if old:
        cleaned = cleaned[: old.start()] + cleaned[old.end() :]
    cleaned = re.sub(
        r"(?:修改为|改成|改为|应该是|应为|实际上是|是)\s*(?=[¥￥]?\d)", "", cleaned
    )
    if re.fullmatch(r"\s*[¥￥]?\d+(?:\.\d{1,2})?\s*元?", cleaned):
        cleaned = "上一笔" + cleaned.strip()
    try:
        date, label, cents, category = parse_expense(cleaned, today)
    except ValueError:
        raise ValueError(
            "我理解你要更正金额，请发“交通费改成14.99”或“11月2日 交通费15改成14.99”；不会新增消费。"
        ) from None
    return date, label, cents, category, old_cents


def classify(text):
    text = text.casefold()
    explicit = [c for c in CATEGORIES if c in text]
    if len(explicit) == 1:
        return explicit[0]
    hits = {c for c, words in WORDS.items() if any(w in text for w in words)}
    return next(iter(hits)) if len(hits) == 1 else None


def parse_expense(text, today):
    """Parse exactly one positive CNY amount, and an optional leading date."""
    text, explicit_category = extract_category(text.strip())
    date = today
    full = re.match(r"^(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?\s*", text)
    partial = re.match(r"^(\d{1,2})[月/-](\d{1,2})日?\s*", text)
    day = re.match(r"^(\d{1,2})[日号]\s*", text)
    if full:
        date = dt.date(*map(int, full.groups()))
        text = text[full.end() :]
    elif partial:
        date = dt.date(today.year, *map(int, partial.groups()))
        text = text[partial.end() :]
    elif day:
        date = dt.date(today.year, today.month, int(day[1]))
        text = text[day.end() :]
    elif text.startswith(("今天", "昨天", "前天")):
        prefix = text[:2]
        date = today - dt.timedelta(days={"今天": 0, "昨天": 1, "前天": 2}[prefix])
        text = text[2:].strip()
    amount = re.fullmatch(r"(.+?)\s*[：:¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?", text)
    if not amount or re.search(r"[\d+\-]", amount[1]):
        raise ValueError(
            "请一次发一笔，例如“午饭20”或“11月3日 盒马6.9”；金额最多两位小数。"
        )
    cents = int(decimal.Decimal(amount[2]) * 100)
    if not 0 < cents <= 100_000_000_00:
        raise ValueError("金额应大于0且不超过一亿元。")
    label = amount[1].strip(" ：:¥￥")
    if not label:
        raise ValueError("请附上消费项目，例如“午饭20”。")
    return date, label, cents, explicit_category or classify(label)


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
                    matches[0].get(k) == v for k, v in values.items()
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


class Robot:
    def __init__(self, store, owner):
        self.store, self.owner = store, owner

    @staticmethod
    def question(row):
        options = "\n".join(
            f"{i}. {CATEGORIES[c]}·{c}" for i, c in enumerate(CATEGORIES, 1)
        )
        return f"{row['日期']} {row['项目']} ¥{row['金额']}，属于哪一类？\n{options}\n回复名称或数字；也可以回复取消。"

    @staticmethod
    def posted_reply(date, row, records, before=None):
        category = row["分类"]
        amount = decimal.Decimal(str(row["金额"]))
        others = [
            r for r in records if r["分类"] == category and r["消息ID"] != row["消息ID"]
        ]
        title = f"{date} {row['项目']}"
        if before is not None:
            reply = f"已修改：{title} ¥{before:.2f} → ¥{amount:.2f}，没有新增消费。"
        else:
            reply = f"已记账：{title} ¥{amount:.2f} → {CATEGORIES[category]}·{category}"
        if others:
            items = "；".join(
                f"{r['项目']} ¥{decimal.Decimal(str(r['金额'])):.2f}" for r in others
            )
            previous = sum(
                (decimal.Decimal(str(r["金额"])) for r in others), decimal.Decimal(0)
            )
            if before is None:
                reply += f"\n原有：{items}\n原有合计：¥{previous:.2f}\n本次新增：{row['项目']} ¥{amount:.2f}\n汇总后（当天{category}）：¥{previous + amount:.2f}"
            else:
                reply += f"\n同日其他消费：{items}\n修改前汇总：¥{previous + before:.2f}\n修改后汇总（当天{category}）：¥{previous + amount:.2f}"
        return reply

    def handle(self, event):
        if (
            event.get("sender_type") != "user"
            or event.get("sender_id") != self.owner
            or event.get("chat_type") != "p2p"
            or event.get("message_type") != "text"
        ):
            return None
        message_id = event.get("message_id")
        if not message_id:
            return None
        text = event["content"].strip()
        today = dt.datetime.fromtimestamp(int(event["create_time"]) / 1000, TZ).date()
        if text in ("帮助", "help", "开始"):
            return HELP
        if re.search(r"图表|仪表盘|饼图|柱状图|支出分析", text) and re.search(
            r"创建|新建|生成|补齐|更新|添加|做", text
        ):
            month_match = re.search(r"(?:(\d{4}|\d{2})年\s*)?(\d{1,2})月", text)
            year_match = re.search(r"(\d{4}|\d{2})年", text)
            year = int(year_match[1]) if year_match else today.year
            year = year + 2000 if year < 100 else year
            if month_match:
                date = dt.date(year, int(month_match[2]), 1)
                self.store.ensure_month(date)
            url = self.store.ensure_charts(year)
            prefix = (
                f"{year}年{int(month_match[2])}月"
                if month_match
                else f"{year}年已有月份"
            )
            return f"已为{prefix}补齐分类支出饼图、每日支出柱状图，并关联全年月度对比。已有图表会复用。\n{url}"
        if ("链接" in text or re.search(r"查看|打开|发给我|给我", text)) and re.search(
            r"账单|账本", text
        ):
            month_match = re.search(r"(?:(\d{4}|\d{2})年\s*)?(\d{1,2})月", text)
            year_match = re.search(r"(\d{4}|\d{2})年", text)
            year, month = today.year, today.month
            if month_match:
                year = int(month_match[1]) if month_match[1] else today.year
                year = year + 2000 if year < 100 else year
                month = int(month_match[2])
            elif "上月" in text or "上个月" in text:
                last = today.replace(day=1) - dt.timedelta(days=1)
                year, month = last.year, last.month
            elif year_match:
                year = int(year_match[1])
                year = year + 2000 if year < 100 else year
                month = None
            dt.date(year, month or 1, 1)
            url = self.store.get_bill_link(year, month)
            label = f"{year}年" + (f"{month}月账单" if month else "账本")
            if not url:
                return f"没有找到{label}。" + (
                    f"可发送“新建{year}年{month}月账单”。"
                    if month
                    else "请先新建该年度的月份账单。"
                )
            return f"{label}链接：\n{url}"
        create = re.fullmatch(
            r"新建\s*(\d{4}|\d{2})年\s*(\d{1,2})月\s*账单(?:表格)?", text
        )
        if create:
            year = int(create[1])
            date = dt.date(year + 2000 if year < 100 else year, int(create[2]), 1)
            token, table = self.store.ensure_month(date)
            try:
                charts = self.store.ensure_charts(date.year)
            except CLIError as exc:
                return f"已准备好{date.year}年{date.month}月账单。\n{self.store.link(token, table)}\n图表尚未补齐：{exc}\n可稍后发送“更新{date.year}年仪表盘”重试，不要重复补记消费。"
            return f"已准备好{date.year}年{date.month}月账单（已存在则复用），并配齐对应月份的饼图、每日柱状图。\n账单：{self.store.link(token, table)}\n支出分析：{charts}"
        processed = self.store.processed(message_id)
        if processed:
            token, row = processed
            if row["状态"] == "待分类":
                return self.question(row)
            if row["状态"] in ("已入账", "已更正"):
                date = dt.date.fromisoformat(row["日期"])
                _, table = self.store.ensure_month(date)
                self.store.sync_day(token, table, date)
            return "这条消息已处理，没有重复记账。"
        correction = parse_correction(text, today)
        if correction:
            date, label, cents, category, old_cents = correction
            token, candidates = self.store.correction_candidates(date, self.owner)
            if label not in ("上一笔", "刚才那笔", "刚才的", "金额"):
                exact = [r for r in candidates if r["项目"] == label]
                candidates = (
                    exact
                    if exact
                    else [r for r in candidates if category and r["分类"] == category]
                )
            if old_cents is not None:
                candidates = [
                    r
                    for r in candidates
                    if int(decimal.Decimal(str(r["金额"])) * 100) == old_cents
                ]
            if len(candidates) != 1:
                return "没有唯一确定要修改的消费，不会新增或累加。请注明日期、项目和原金额，例如“11月2日 交通费15改成14.99”。"
            row = candidates[0]
            before = decimal.Decimal(str(row["金额"]))
            _, table = self.store.ensure_month(date)
            self.store.write(token, "消费明细", {"金额": cents / 100}, row["record_id"])
            # A non-posted audit row preserves the old amount and makes correction
            # event replays idempotent, without replacing earlier reply receipts.
            self.store.write(
                token,
                "消费明细",
                {
                    "消息ID": message_id,
                    "日期": date.isoformat(),
                    "项目": f"更正{row['项目']}：{before:.2f}→{cents / 100:.2f}",
                    "金额": cents / 100,
                    "分类": row["分类"],
                    "大类": CATEGORIES[row["分类"]],
                    "状态": "已更正",
                    "发送者": self.owner,
                },
            )
            records = self.store.sync_day(token, table, date)
            return self.posted_reply(date, dict(row, 金额=cents / 100), records, before)
        pending = self.store.pending(self.owner)
        if pending:
            token, row = pending[0]
            if text == "取消":
                self.store.write(
                    token,
                    "消费明细",
                    {"状态": "已取消", "分类回复ID": message_id},
                    row["record_id"],
                )
                return "已取消这笔待分类消费。"
            category = ALIASES.get(text, text)
            body, explicit_category = extract_category(text)
            if explicit_category:
                if not body:
                    category = explicit_category
                else:
                    # An annotated resend of the same pending expense completes
                    # that record; a different expense must not classify it.
                    parsed_date, label, cents, _ = parse_expense(text, today)
                    if (parsed_date.isoformat(), label, cents) == (
                        row["日期"],
                        row["项目"],
                        int(decimal.Decimal(str(row["金额"])) * 100),
                    ):
                        category = explicit_category
            if text in [str(i) for i in range(1, 10)]:
                category = list(CATEGORIES)[int(text) - 1]
            if category not in CATEGORIES:
                return "请先给上一笔消费选择分类。\n" + self.question(row)
            date = dt.date.fromisoformat(row["日期"])
            _, table = self.store.ensure_month(date)
            self.store.write(
                token,
                "消费明细",
                {
                    "分类": category,
                    "大类": CATEGORIES[category],
                    "状态": "已入账",
                    "分类回复ID": message_id,
                },
                row["record_id"],
            )
            records = self.store.sync_day(token, table, date)
            return self.posted_reply(date, dict(row, 分类=category), records)
        date, label, cents, category = parse_expense(text, today)
        token, table = self.store.ensure_month(date)
        existing = self.store.records(token, "消费明细", [["消息ID", "==", message_id]])
        if existing:
            row = existing[0]
            if row["状态"] == "待分类":
                return self.question(row)
            if row["状态"] == "已入账":
                self.store.sync_day(token, table, date)
            return "这条消息已处理，没有重复记账。"
        row = {
            "消息ID": message_id,
            "日期": date.isoformat(),
            "项目": label,
            "金额": cents / 100,
            "分类": category or "",
            "大类": CATEGORIES.get(category, ""),
            "状态": "已入账" if category else "待分类",
            "发送者": self.owner,
        }
        self.store.write(token, "消费明细", row)
        if not category:
            return self.question(row)
        records = self.store.sync_day(token, table, date)
        return self.posted_reply(date, row, records)


def run():
    root = Path(__file__).resolve().parent
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
    robot = Robot(FeishuStore(cli, config, path), owner)
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
                except ValueError as exc:
                    reply = str(exc)
                except CLIError as exc:
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
            except (CLIError, ValueError, KeyError, subprocess.TimeoutExpired):
                logging.exception("消息处理失败")
        code = child.wait()
        if not stop.is_set():
            logging.warning("事件监听退出（%s），10秒后重连", code)
            stop.wait(10)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    run()
