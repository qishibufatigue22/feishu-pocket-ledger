import copy
import datetime as dt
import unittest

from robot import (
    CATEGORIES,
    FeishuStore,
    Robot,
    classify,
    decode_records,
    parse_expense,
)
from unittest.mock import Mock, patch


class FakeStore:
    def __init__(self):
        self.rows = []
        self.months = {}
        self.totals = {}
        self.chart_years = []
        self.completed = set()
        self.config = {"years": {}}

    def is_completed(self, message_id):
        return message_id in self.completed

    def mark_completed(self, message_id):
        self.completed.add(message_id)

    def ensure_month(self, date):
        self.config["years"][str(date.year)] = str(date.year)
        key = (date.year, date.month)
        self.months.setdefault(key, (str(date.year), str(date.month)))
        return self.months[key]

    def link(self, token, table):
        return f"https://example.test/base/{token}?table={table}"

    def get_bill_link(self, year, month):
        if (year, month) in self.months:
            return self.link(*self.months[(year, month)])
        return None

    def ensure_charts(self, year):
        self.chart_years.append(year)
        return f"https://example.test/base/{year}?table=dashboard"

    def records(self, token, table, conditions=None):
        return [
            copy.deepcopy(r)
            for r in self.rows
            if r["日期"].startswith(token)
            and all(r.get(k) == v for k, _, v in conditions or [])
        ]

    def write(self, token, table, values, record_id=None):
        if record_id:
            next(r for r in self.rows if r["record_id"] == record_id).update(values)
        else:
            self.rows.append(dict(values, record_id=str(len(self.rows))))

    def pending(self, sender):
        return [
            (r["日期"][:4], copy.deepcopy(r))
            for r in self.rows
            if r["状态"] == "待分类" and r["发送者"] == sender
        ]

    def processed(self, message_id):
        for r in self.rows:
            if message_id in (r.get("消息ID"), r.get("分类回复ID")):
                return r["日期"][:4], copy.deepcopy(r)

    def sync_day(self, token, table, date):
        self.totals[str(date)] = sum(
            round(r["金额"] * 100)
            for r in self.rows
            if r["日期"] == str(date) and r["状态"] == "已入账"
        )
        return [
            copy.deepcopy(r)
            for r in self.rows
            if r["日期"] == str(date) and r["状态"] == "已入账"
        ]

    def correction_candidates(self, date, sender):
        return str(date.year), [
            copy.deepcopy(r)
            for r in self.rows
            if r["日期"] == str(date)
            and r["状态"] == "已入账"
            and r["发送者"] == sender
        ]


class Tests(unittest.TestCase):
    def setUp(self):
        self.today = dt.date(2026, 11, 2)
        self.store = FakeStore()
        self.robot = Robot(self.store, "ou_me")
        self.counter = 0

    def event(self, text):
        self.counter += 1
        return {
            "sender_id": "ou_me",
            "sender_type": "user",
            "chat_type": "p2p",
            "message_type": "text",
            "message_id": f"om_{self.counter}",
            "content": text,
            "create_time": "1793610000000",
        }

    def test_natural_batch_uses_shared_date_and_stable_ids(self):
        event = self.event("昨天打车36，咖啡22，帮我记一下。")
        result = self.robot.handle(event)
        self.assertIn("已记账", result)
        self.assertEqual(
            [(row["日期"], row["项目"], row["金额"]) for row in self.store.rows],
            [("2026-11-01", "打车", 36), ("2026-11-01", "咖啡", 22)],
        )
        self.robot.handle(event)
        self.assertEqual(len(self.store.rows), 2)

    def test_natural_query_uses_only_own_posted_records(self):
        self.robot.handle(self.event("10月28午饭22"))
        self.robot.handle(self.event("10月29午饭30"))
        self.robot.handle(self.event("10月29打车50"))
        self.store.rows += [
            dict(self.store.rows[0], 状态="已撤销", 金额=500),
            dict(self.store.rows[0], 发送者="ou_other", 金额=999),
        ]
        result = self.robot.handle(self.event("上周吃饭花了多少？哪天最多？"))
        self.assertIn("¥52.00", result)
        self.assertIn("2026-10-29，¥30.00", result)
        self.assertEqual(len(self.store.rows), 5)

    def test_natural_correction_does_not_change_other_transport(self):
        self.robot.handle(self.event("昨天公交3"))
        result = self.robot.handle(self.event("昨天那笔打车改成32"))
        self.assertIn("没有唯一确定", result)
        self.assertEqual(self.store.rows[0]["金额"], 3)
        self.robot.handle(self.event("昨天打车36"))
        result = self.robot.handle(self.event("昨天那笔打车改成32"))
        self.assertIn("已修改", result)
        self.assertEqual(self.store.rows[1]["金额"], 32)

    def test_natural_batch_validates_before_writing(self):
        result = self.robot.handle(self.event("帮我记昨天打车36，盒马22"))
        self.assertIn("尚未入账", result)
        result = self.robot.handle(self.event("昨天打车36可以记吗？"))
        self.assertIn("未修改账单", result)
        self.assertFalse(self.store.rows)

    def test_implicit_date_and_decimal(self):
        self.assertEqual(
            parse_expense("盒马6.9", self.today), (self.today, "盒马", 690, None)
        )

    def test_month_footer_from_posted_details(self):
        store = FeishuStore(None, {}, None)
        facts = [
            {"日期": "2026-10-01", "分类": "交通", "金额": 14.99},
            {"日期": "2026-10-30", "分类": "交通", "金额": 1.09},
            {"日期": "2026-10-30", "分类": "伙食费", "金额": 78.5},
            {"日期": "2026-11-01", "分类": "交通", "金额": 100},
        ]
        for row in facts:
            row["状态"] = "已入账"
        store.records = Mock(
            side_effect=[facts, [], facts, [{"record_id": "rec_footer"}]]
        )
        store.write = Mock()
        store.sync_month("token", "october", dt.date(2026, 10, 1))
        first = store.write.call_args.args
        self.assertEqual(first[2]["日期"], "总计")
        self.assertEqual(first[2]["流动费·交通"], 16.08)
        self.assertEqual(first[2]["当日合计"], 94.58)
        self.assertIsNone(first[2]["固定费·基金"])
        self.assertIsNone(first[3])
        self.assertEqual(
            store.records.call_args_list[0].args[2], [["状态", "==", "已入账"]]
        )
        store.sync_month("token", "october", dt.date(2026, 10, 1))
        self.assertEqual(store.write.call_args.args[3], "rec_footer")

    def test_zero_day_and_month_are_cleared(self):
        store = FeishuStore(None, {}, None)
        store.records = Mock(
            side_effect=[[], [{"record_id": "day"}], [{"record_id": "footer"}]]
        )
        store.write = Mock()
        store.sync_day("token", "november", self.today)
        self.assertEqual(store.write.call_count, 2)
        for call in store.write.call_args_list:
            values = call.args[2]
            self.assertIsNone(values["当日合计"])
            for category, group in CATEGORIES.items():
                self.assertIsNone(values[f"{group}·{category}"])

    def test_write_waits_until_new_expense_is_visible(self):
        store = FeishuStore(Mock(), {}, None)
        old = {
            "record_id": "old",
            "消息ID": "m1",
            "日期": "2026-10-21",
            "金额": 94,
            "分类": "交通",
            "状态": "已入账",
        }
        new = {
            "record_id": "new",
            "消息ID": "m2",
            "日期": "2026-10-21",
            "金额": 179,
            "分类": "交通",
            "状态": "已入账",
        }
        store.records = Mock(side_effect=[[old], [old, new]])
        with patch("storage.feishu.time.sleep") as sleep:
            store.write(
                "token", "消费明细", {k: v for k, v in new.items() if k != "record_id"}
            )
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(store.confirmed_details["token"], [old, new])
        # Even if a subsequent read would be stale, both totals use the
        # confirmed snapshot containing the two distinct expenses.
        store.records = Mock(
            side_effect=[[{"record_id": "day"}], [{"record_id": "footer"}]]
        )
        store.write = Mock()
        store.sync_day("token", "october", dt.date(2026, 10, 21))
        for call in store.write.call_args_list:
            self.assertEqual(call.args[2]["流动费·交通"], 273)
            self.assertEqual(call.args[2]["当日合计"], 273)

    def test_explicit_trailing_category(self):
        for text in [
            "10月30乳胶手套3.08（日用品）",
            "10月30乳胶手套3.08 (日用品)",
            "10月30乳胶手套3.08属于日用品",
            "10月30乳胶手套3.08，属于日用品",
            "10月30乳胶手套3.08元（流动费·日用品）",
        ]:
            with self.subTest(text=text):
                self.assertEqual(
                    parse_expense(text, self.today),
                    (dt.date(2026, 10, 30), "乳胶手套", 308, "日用品"),
                )

    def test_declared_category_overrides_keywords(self):
        self.assertEqual(parse_expense("外卖20（零花钱）", self.today)[3], "零花钱")
        with self.assertRaises(ValueError):
            parse_expense("手套3.08（未知分类）", self.today)

    def test_explicit_category_posts_without_question(self):
        reply = self.robot.handle(self.event("10月30乳胶手套3.08（日用品）"))
        self.assertIn("已记账", reply)
        self.assertEqual(self.store.rows[0]["项目"], "乳胶手套")
        self.assertEqual(self.store.rows[0]["分类"], "日用品")
        self.assertEqual(self.store.totals["2026-10-30"], 308)

    def test_category_declaration_answers_pending(self):
        self.robot.handle(self.event("10月30收纳盒3.08"))
        self.robot.handle(self.event("属于日用品"))
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.rows[0]["状态"], "已入账")

    def test_annotated_resend_completes_same_pending(self):
        self.robot.handle(self.event("10月30收纳盒3.08"))
        self.robot.handle(self.event("10月30收纳盒3.08（日用品）"))
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.rows[0]["分类"], "日用品")

    def test_other_annotated_expense_does_not_classify_pending(self):
        self.robot.handle(self.event("盒马6.9"))
        reply = self.robot.handle(self.event("乳胶手套3.08（日用品）"))
        self.assertIn("请先", reply)
        self.assertEqual(self.store.rows[0]["状态"], "待分类")
        self.assertEqual(len(self.store.rows), 1)

    def test_dates(self):
        for text, expected in [
            ("11月3日 午饭20", dt.date(2026, 11, 3)),
            ("2025-10-03 午饭20", dt.date(2025, 10, 3)),
            ("昨天 午饭20", dt.date(2026, 11, 1)),
            ("3日 午饭20", dt.date(2026, 11, 3)),
        ]:
            self.assertEqual(parse_expense(text, self.today)[0], expected)

    def test_invalid_inputs(self):
        for text in [
            "午饭-3",
            "午饭0",
            "午饭1.234",
            "午饭20 晚饭30",
            "2026-02-30 午饭20",
            "20",
        ]:
            with self.assertRaises(ValueError, msg=text):
                parse_expense(text, self.today)

    def test_classification(self):
        self.assertEqual(classify("卫生纸"), "日用品")
        self.assertIsNone(classify("盒马"))
        self.assertIsNone(classify("买菜和卫生纸"))
        self.assertEqual(len(CATEGORIES), 9)

    def test_digital_subscriptions_are_pocket_money(self):
        for item in (
            "iCloud",
            "icloud",
            "ICLOUD",
            "GPT",
            "gpt",
            "ChatGPT",
            "chatgpt订阅",
        ):
            with self.subTest(item=item):
                self.assertEqual(parse_expense(item + "28", self.today)[3], "零花钱")

    def test_common_transport_and_daily_expenses(self):
        for label in (
            "高铁",
            "动车",
            "火车票",
            "机票",
            "网约车",
            "顺风车",
            "哈啰单车",
            "轮渡",
            "高速费",
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    parse_expense(f"10月23 {label}36", self.today)[3], "交通"
                )
        reply = self.robot.handle(self.event("10月23 高铁36"))
        self.assertIn("已记账", reply)
        self.assertEqual(self.store.rows[0]["分类"], "交通")
        self.assertEqual(self.store.totals["2026-10-23"], 3600)
        for label, category in [
            ("食堂", "伙食费"),
            ("乳胶手套", "日用品"),
            ("抽纸", "日用品"),
            ("薯片", "零花钱"),
        ]:
            self.assertEqual(classify(label), category)
        self.assertEqual(parse_expense("高铁36（旅行）", self.today)[3], "旅行")
        self.assertIsNone(classify("超市"))

    def test_restaurant_and_shared_bike_post_directly(self):
        for text, label, category, cents in [
            ("10月30 福万越南餐厅78.5", "福万越南餐厅", "伙食费", 7850),
            ("10月30 共享单车1.09", "共享单车", "交通", 109),
        ]:
            with self.subTest(text=text):
                reply = self.robot.handle(self.event(text))
                self.assertIn("已记账", reply)
                row = self.store.rows[-1]
                self.assertEqual(
                    (row["项目"], row["分类"], round(row["金额"] * 100)),
                    (label, category, cents),
                )
        self.assertEqual(self.store.totals["2026-10-30"], 7959)
        self.assertEqual(
            parse_expense("共享单车1.09（零花钱）", self.today)[3], "零花钱"
        )

    def test_year_and_month_reuse(self):
        self.robot.handle(self.event("新建2026年11月账单"))
        self.robot.handle(self.event("新建2026年10月账单表格"))
        self.robot.handle(self.event("新建2026年11月账单"))
        self.robot.handle(self.event("新建2027年1月账单"))
        self.assertEqual(len(self.store.months), 3)
        self.assertEqual(
            self.store.months[(2026, 11)][0], self.store.months[(2026, 10)][0]
        )
        self.assertNotEqual(
            self.store.months[(2026, 11)][0], self.store.months[(2027, 1)][0]
        )
        self.assertEqual(self.store.chart_years, [2026, 2026, 2026, 2027])

    def test_create_month_charts_and_update_dashboard_commands(self):
        reply = self.robot.handle(self.event("创建26年9月图表"))
        self.assertIn((2026, 9), self.store.months)
        self.assertIn("2026年9月", reply)
        self.assertIn("table=dashboard", reply)
        self.robot.handle(self.event("更新仪表盘"))
        self.assertEqual(self.store.chart_years, [2026, 2026])
        self.assertEqual(len(self.store.months), 1)
        self.assertFalse(self.store.rows)

    def test_bill_links_accept_short_year_and_do_not_post(self):
        self.robot.handle(self.event("新建2026年10月账单"))
        for text in (
            "把26年10月账单链接发给我",
            "给我2026年10月账单链接",
            "查看10月账单",
            "把上个月账单链接发给我",
        ):
            with self.subTest(text=text):
                reply = self.robot.handle(self.event(text))
                self.assertIn("2026年10月账单链接", reply)
                self.assertIn("https://example.test/base/2026?table=10", reply)
        reply = self.robot.handle(self.event("把2027年2月账单链接发给我"))
        self.assertIn("没有找到", reply)
        self.assertNotIn((2027, 2), self.store.months)
        self.assertFalse(self.store.rows)

    def test_repeated_event_not_added_twice(self):
        event = self.event("午饭20")
        self.robot.handle(event)
        self.robot.handle(event)
        self.robot.handle(self.event("晚饭30"))
        self.assertEqual(len(self.store.rows), 2)
        self.assertEqual(list(self.store.totals.values()), [5000])

    def test_delayed_duplicate_with_different_id_is_not_posted(self):
        self.robot.handle(self.event("10月24 iCloud28"))
        reply = self.robot.handle(self.event("10月24 iCloud28"))
        self.assertIn("疑似重复，未新增", reply)
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.totals["2026-10-24"], 2800)
        self.robot.handle(self.event("再记一笔 10月24 iCloud28"))
        self.assertEqual(len(self.store.rows), 2)
        self.assertEqual(self.store.totals["2026-10-24"], 5600)

    def test_remove_duplicate_shows_before_and_after(self):
        self.robot.handle(self.event("10月24 修正带4.73（零花钱）"))
        self.robot.handle(self.event("10月24 iCloud28"))
        self.robot.handle(self.event("再记一笔 10月24 iCloud28"))
        event = self.event("不对 10月24 iCloud重复了")
        reply = self.robot.handle(event)
        self.assertIn("原有合计：¥60.73", reply)
        self.assertIn("本次扣除：iCloud ¥28.00", reply)
        self.assertIn("调整后（当天零花钱）：¥32.73", reply)
        self.assertEqual(self.store.totals["2026-10-24"], 3273)
        self.robot.handle(event)
        self.assertEqual(self.store.totals["2026-10-24"], 3273)
        reply = self.robot.handle(self.event("10月24 iCloud重复了"))
        self.assertIn("只有一笔", reply)
        self.assertEqual(self.store.totals["2026-10-24"], 3273)

    def test_explicit_withdrawal_can_remove_single_expense(self):
        self.robot.handle(self.event("10月24 iCloud28"))
        reply = self.robot.handle(self.event("撤销10月24 iCloud28"))
        self.assertIn("调整后（当天零花钱）：¥0.00", reply)
        self.assertEqual(self.store.totals["2026-10-24"], 0)

    def test_completed_commands_are_not_replied_to_again(self):
        event = self.event("新建9月账单")
        self.assertIn("2026年9月", self.robot.handle(event))
        self.store.mark_completed(event["message_id"])
        restarted = Robot(self.store, "ou_me")
        self.assertIsNone(restarted.handle(event))
        self.assertEqual(len(self.store.chart_years), 1)

    def test_completed_message_ids_are_persisted(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = FeishuStore(None, {"years": {}}, path)
            store.mark_completed("om_done")
            import json

            restarted = FeishuStore(None, json.loads(path.read_text()), path)
            self.assertTrue(restarted.is_completed("om_done"))

    def test_empty_text_normalized_to_null_is_confirmed(self):
        store = FeishuStore(Mock(), {}, None)
        store.records = Mock(return_value=[{
            "record_id": "rec_pending", "消息ID": "om_pending",
            "分类": None, "大类": None, "状态": "待分类",
        }])
        with patch("storage.feishu.time.sleep") as sleep:
            store.write("token", "消费明细", {
                "消息ID": "om_pending", "分类": "", "大类": "", "状态": "待分类",
            })
        sleep.assert_not_called()
        self.assertIn("token", store.confirmed_details)

    def test_aggregation_reply_shows_previous_and_new_expenses(self):
        self.robot.handle(self.event("10月21 高铁94"))
        self.robot.handle(self.event("10月21 午饭20"))
        reply = self.robot.handle(self.event("10月21 高铁179"))
        self.assertIn("原有：高铁 ¥94.00", reply)
        self.assertIn("原有合计：¥94.00", reply)
        self.assertIn("本次新增：高铁 ¥179.00", reply)
        self.assertIn("汇总后（当天交通）：¥273.00", reply)
        self.assertNotIn("午饭", reply)

    def test_pending_classification_reply_shows_aggregation(self):
        self.robot.handle(self.event("午饭20"))
        self.robot.handle(self.event("盒马6.9"))
        reply = self.robot.handle(self.event("伙食费"))
        self.assertIn("原有：午饭 ¥20.00", reply)
        self.assertIn("汇总后（当天伙食费）：¥26.90", reply)

    def test_correction_reply_shows_category_total(self):
        self.robot.handle(self.event("高铁94"))
        self.robot.handle(self.event("公交3"))
        reply = self.robot.handle(self.event("高铁改成90"))
        self.assertIn("同日其他消费：公交 ¥3.00", reply)
        self.assertIn("修改前汇总：¥97.00", reply)
        self.assertIn("修改后汇总（当天交通）：¥93.00", reply)

    def test_correction_replaces_original_amount(self):
        self.robot.handle(self.event("交通费15"))
        correction = self.event("不对，交通费是14.99")
        reply = self.robot.handle(correction)
        self.assertIn("已修改", reply)
        self.assertEqual(self.store.rows[0]["金额"], 14.99)
        self.assertEqual(list(self.store.totals.values()), [1499])
        self.assertEqual(len([r for r in self.store.rows if r["状态"] == "已入账"]), 1)
        self.robot.handle(self.event("交通费改成12"))
        self.robot.handle(correction)
        self.assertEqual(self.store.rows[0]["金额"], 12)
        self.assertEqual(list(self.store.totals.values()), [1200])

    def test_ambiguous_correction_does_not_add_expense(self):
        self.robot.handle(self.event("交通费15"))
        self.robot.handle(self.event("交通费3"))
        reply = self.robot.handle(self.event("不对，交通费是14.99"))
        self.assertIn("没有唯一确定", reply)
        self.assertEqual(len(self.store.rows), 2)
        self.assertEqual(list(self.store.totals.values()), [1800])
        self.robot.handle(self.event("11月2日 交通费15改成14.99"))
        self.assertEqual(self.store.rows[0]["金额"], 14.99)
        self.assertEqual(self.store.rows[1]["金额"], 3)
        self.assertEqual(list(self.store.totals.values()), [1799])

    def test_unmatched_correction_never_creates_record(self):
        self.robot.handle(self.event("不对，交通费是14.99"))
        self.assertFalse(self.store.rows)

    def test_bad_correction_does_not_create_record(self):
        self.robot.handle(self.event("交通费15"))
        with self.assertRaises(ValueError):
            self.robot.handle(self.event("不对，交通费改成14.999"))
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.rows[0]["金额"], 15)

    def test_pending_and_reply_replay(self):
        original = self.event("2026-10-03 盒马6.9")
        self.robot.handle(original)
        self.robot.handle(original)
        self.assertEqual(len(self.store.rows), 1)
        self.assertFalse(self.store.totals)
        answer = self.event("日用品")
        self.robot.handle(answer)
        self.robot.handle(self.event("超市10"))
        self.robot.handle(answer)  # Old reply must never classify a newer expense.
        self.assertEqual(self.store.rows[1]["状态"], "待分类")
        self.assertEqual(self.store.totals["2026-10-03"], 690)

    def test_cancel(self):
        self.robot.handle(self.event("盒马6.9"))
        self.robot.handle(self.event("取消"))
        self.assertEqual(self.store.rows[0]["状态"], "已取消")
        self.assertFalse(self.store.totals)

    def test_owner_only(self):
        event = self.event("午饭20")
        event["sender_id"] = "ou_other"
        self.assertIsNone(self.robot.handle(event))
        self.assertFalse(self.store.rows)

    def test_fail_closed_on_unknown_record_format(self):
        with self.assertRaises(RuntimeError):
            decode_records({"unexpected": []})
        self.assertEqual(
            decode_records(
                {"records": [{"record_id": "r1", "fields": {"日期": "2026-11-02"}}]}
            ),
            [{"record_id": "r1", "日期": "2026-11-02"}],
        )

    def test_real_cli_matrix(self):
        data = {
            "data": [["2026-11-02", 6.9]],
            "fields": ["日期", "金额"],
            "record_id_list": ["rec_1"],
        }
        self.assertEqual(
            decode_records(data),
            [{"日期": "2026-11-02", "金额": 6.9, "record_id": "rec_1"}],
        )
        self.assertEqual(
            decode_records({"data": [], "fields": ["日期"], "record_id_list": []}), []
        )


if __name__ == "__main__":
    unittest.main()
