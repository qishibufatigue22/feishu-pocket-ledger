import datetime as dt
import decimal
import re
from knowledge.categories import CATEGORIES, ALIASES, HELP, TZ
from agent.intents import extract_category, parse_correction, parse_removal, parse_expense, expense_parts, query_intent
from feishu_cli import CLIError

from tools.analytics import AnalyticsTools


class LedgerTools(AnalyticsTools):
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


    def handle(self, event, *, routed=False):
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
        if self.store.is_completed(message_id):
            return None
        text = event["content"].strip()
        today = dt.datetime.fromtimestamp(int(event["create_time"]) / 1000, TZ).date()
        if not routed:
            query = query_intent(text, today, CATEGORIES)
            if query:
                return self.spending_reply(query)
            if re.search(r"[？?]|是不是|要不要|能不能|可以.*吗", text):
                return "这句话像是在询问，未修改账单。要记账可以说“帮我记昨天打车36”。"
            parts = expense_parts(text)
            if parts:
                parsed = [parse_expense(part, today) for part in parts]
                if len(parts) > 1:
                    if any(not entry[3] for entry in parsed):
                        return "有项目的分类不明确，整批尚未入账。请在金额后注明分类，例如“咖啡22（伙食费）”。"
                    if self.store.pending(self.owner):
                        return "请先处理上一笔待分类消费，再发这批消费。"
                    return "\n\n".join(
                        self.handle(dict(event, content=part, message_id=f"{message_id}:{i}"), routed=True)
                        for i, part in enumerate(parts, 1)
                    )
                text = parts[0]
                routed = True
            text = re.sub(r"^(?:请)?(?:帮我)?(?:把|将)?(今天|昨天|前天)\s*(?:的)?那笔\s*", r"\1 ", text)
            text = re.sub(r"^(?:请)?帮我(把|将)(?=.+(?:改成|改为))", r"\1", text)
        if text in ("帮助", "help", "开始"):
            return HELP
        if not routed and re.search(r"图表|仪表盘|饼图|柱状图|支出分析", text) and re.search(
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
        if not routed and ("链接" in text or re.search(r"查看|打开|发给我|给我", text)) and re.search(
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
            r"新建\s*(?:(\d{4}|\d{2})年\s*)?(\d{1,2})月\s*账单(?:表格)?", text
        )
        if create and not routed:
            year = int(create[1]) if create[1] else today.year
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
            if row["状态"] in ("已入账", "已更正", "撤销记录"):
                date = dt.date.fromisoformat(row["日期"])
                _, table = self.store.ensure_month(date)
                self.store.sync_day(token, table, date)
            return "这条消息已处理，没有重复记账。"
        removal = None if routed else parse_removal(text, today)
        if removal:
            date, label, cents, duplicate = removal
            token, posted = self.store.correction_candidates(date, self.owner)
            candidates = [
                r
                for r in posted
                if r["项目"].strip().casefold() == label.strip().casefold()
            ]
            if cents is not None:
                candidates = [
                    r
                    for r in candidates
                    if int(decimal.Decimal(str(r["金额"])) * 100) == cents
                ]
            amounts = {decimal.Decimal(str(r["金额"])) for r in candidates}
            if event.get("_selected_id"):
                candidates = [r for r in candidates if r["record_id"] == event["_selected_id"]]
                amounts = {decimal.Decimal(str(r["金额"])) for r in candidates}
            if len(candidates) > 1 and len(amounts) > 1:
                return self.wait_for_selection(event, date, label, candidates, "撤销")
            if not candidates or len(amounts) > 1:
                return "没有唯一确定要扣除的消费，未调整账单。请注明日期、项目及金额，例如“撤销10月24 iCloud28”。"
            if duplicate and len(candidates) < 2:
                return f"{date} {label}目前只有一笔已入账记录，未再次扣减。如需删除它，请说“撤销{date} {label}{candidates[0]['金额']}”。"
            row = max(candidates, key=lambda r: r["record_id"])
            category = row["分类"]
            originals = [r for r in posted if r["分类"] == category]
            before = sum(
                (decimal.Decimal(str(r["金额"])) for r in originals), decimal.Decimal(0)
            )
            amount = decimal.Decimal(str(row["金额"]))
            _, table = self.store.ensure_month(date)
            self.store.write(token, "消费明细", {"状态": "已撤销"}, row["record_id"])
            self.store.write(
                token,
                "消费明细",
                {
                    "消息ID": message_id,
                    "日期": date.isoformat(),
                    "项目": f"撤销{row['项目']} ¥{amount:.2f}（{row['record_id']}）",
                    "金额": float(amount),
                    "分类": category,
                    "大类": CATEGORIES[category],
                    "状态": "撤销记录",
                    "发送者": self.owner,
                },
            )
            records = self.store.sync_day(token, table, date)
            after = sum(
                (
                    decimal.Decimal(str(r["金额"]))
                    for r in records
                    if r["分类"] == category
                ),
                decimal.Decimal(0),
            )
            items = "；".join(
                f"{r['项目']} ¥{decimal.Decimal(str(r['金额'])):.2f}" for r in originals
            )
            return f"已撤销一笔：{date} {row['项目']} ¥{amount:.2f}\n原有：{items}\n原有合计：¥{before:.2f}\n本次扣除：{row['项目']} ¥{amount:.2f}\n调整后（当天{category}）：¥{after:.2f}"
        correction = None if routed else parse_correction(text, today)
        if correction:
            date, label, cents, category, old_cents = correction
            token, candidates = self.store.correction_candidates(date, self.owner)
            if label not in ("上一笔", "刚才那笔", "刚才的", "金额"):
                candidates = [
                    r for r in candidates
                    if r["项目"].casefold() == label.casefold()
                    or (label in CATEGORIES and r["分类"] == label)
                ]
            if old_cents is not None:
                candidates = [
                    r
                    for r in candidates
                    if int(decimal.Decimal(str(r["金额"])) * 100) == old_cents
                ]
            if event.get("_selected_id"):
                candidates = [r for r in candidates if r["record_id"] == event["_selected_id"]]
            if len(candidates) > 1:
                return self.wait_for_selection(event, date, label, candidates, "更正", cents / 100)
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
        repeated = re.match(r"^(?:再记一笔|再记|仍要新增)[：:\s]*", text)
        expense_text = text[repeated.end() :] if repeated else text
        date, label, cents, category = parse_expense(expense_text, today)
        token, table = self.store.ensure_month(date)
        existing = self.store.records(token, "消费明细", [["消息ID", "==", message_id]])
        if existing:
            row = existing[0]
            if row["状态"] == "待分类":
                return self.question(row)
            if row["状态"] == "已入账":
                self.store.sync_day(token, table, date)
            return "这条消息已处理，没有重复记账。"
        if not repeated:
            _, posted = self.store.correction_candidates(date, self.owner)
            duplicates = [
                r
                for r in posted
                if r["项目"].strip().casefold() == label.strip().casefold()
                and int(decimal.Decimal(str(r["金额"])) * 100) == cents
            ]
            if duplicates:
                category = duplicates[0]["分类"]
                prior = [r for r in posted if r["分类"] == category]
                items = "；".join(
                    f"{r['项目']} ¥{decimal.Decimal(str(r['金额'])):.2f}" for r in prior
                )
                total = sum(
                    (decimal.Decimal(str(r["金额"])) for r in prior), decimal.Decimal(0)
                )
                return f"疑似重复，未新增：{date} {label} ¥{cents / 100:.2f}\n原有：{items}\n原有合计：¥{total:.2f}\n本次新增：¥0.00\n当前（当天{category}）：¥{total:.2f}\n如果确实又消费了一笔，请发“再记一笔 {expense_text}”。"
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
