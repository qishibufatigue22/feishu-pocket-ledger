import datetime as dt
import unittest
from agent.orchestrator import Robot
from storage.state import MemoryState
from feishu_cli import CLIError
from unittest.mock import patch
from test_robot import FakeStore


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.state = MemoryState()
        self.robot = Robot(self.store, "ou_me", state=self.state)
        self.counter = 0

    def event(self, text):
        self.counter += 1
        return {"sender_id": "ou_me", "sender_type": "user", "chat_type": "p2p",
                "message_type": "text", "message_id": f"agent_{self.counter}",
                "content": text, "create_time": "1793610000000"}

    def test_resume_selection_after_restart(self):
        self.robot.handle(self.event("昨天打车36"))
        self.robot.handle(self.event("昨天打车32"))
        result = self.robot.handle(self.event("昨天那笔打车改成30"))
        self.assertIn("请选择", result)
        restarted = Robot(self.store, "ou_me", state=self.state)
        result = restarted.handle(self.event("第二笔"))
        self.assertIn("已修改", result)
        self.assertEqual([r["金额"] for r in self.store.rows[:2]], [36, 30])
        self.assertIsNone(self.state.load()["task"])

    def test_revalidate_before_modification(self):
        self.robot.handle(self.event("昨天打车36"))
        self.robot.handle(self.event("昨天打车32"))
        self.robot.handle(self.event("昨天打车改成30"))
        self.store.rows[1]["金额"] = 99
        result = self.robot.handle(self.event("第二笔"))
        self.assertIn("已发生变化", result)
        self.assertEqual(self.store.rows[1]["金额"], 99)

    def test_cancel_does_not_change_ledger(self):
        self.robot.handle(self.event("昨天打车36"))
        self.robot.handle(self.event("昨天打车32"))
        self.robot.handle(self.event("昨天打车改成30"))
        self.robot.handle(self.event("取消这个操作"))
        self.assertEqual(len(self.store.rows), 2)
        self.assertIsNone(self.state.load()["task"])

    def test_amount_selection_and_withdrawal(self):
        self.robot.handle(self.event("昨天打车36"))
        self.robot.handle(self.event("昨天打车32"))
        result = self.robot.handle(self.event("撤销昨天打车"))
        self.assertIn("请选择", result)
        result = self.robot.handle(self.event("36元的"))
        self.assertIn("已撤销一笔", result)
        self.assertEqual(self.store.rows[0]["状态"], "已撤销")
        self.assertEqual(self.store.rows[1]["状态"], "已入账")

    def test_rule_requires_confirmation_and_survives_restart(self):
        self.robot.handle(self.event("昨天盒马22"))
        reply = self.robot.handle(self.event("伙食费"))
        self.assertIn("要记住", reply)
        self.assertFalse(self.state.load()["rules"])
        self.robot.handle(self.event("记住"))
        restarted = Robot(self.store, "ou_me", state=self.state)
        reply = restarted.handle(self.event("盒马25"))
        self.assertIn("已记账", reply)
        self.assertEqual(self.store.rows[-1]["分类"], "伙食费")
        restarted.handle(self.event("忘记分类规则盒马"))
        self.assertFalse(self.state.load()["rules"])

    def test_interrupted_execution_resumes_without_second_withdrawal(self):
        self.robot.handle(self.event("昨天打车36"))
        self.robot.handle(self.event("昨天打车32"))
        self.robot.handle(self.event("撤销昨天打车"))
        reply = self.event("36元的")
        with patch.object(self.store, "sync_day", side_effect=CLIError("暂时失败")):
            with self.assertRaises(CLIError):
                self.robot.handle(reply)
        self.assertEqual(self.state.load()["task"]["status"], "executing")
        restarted = Robot(self.store, "ou_me", state=self.state)
        result = restarted.handle(reply)
        self.assertIn("这条消息已处理", result)
        self.assertIsNone(self.state.load()["task"])
        self.assertEqual(sum(r["状态"] == "已撤销" for r in self.store.rows), 1)

    def test_other_user_cannot_select_personal_task(self):
        self.robot.handle(self.event("昨天打车36"))
        self.robot.handle(self.event("昨天打车32"))
        self.robot.handle(self.event("昨天打车改成30"))
        event = dict(self.event("第二笔"), sender_id="ou_other")
        self.assertIsNone(self.robot.handle(event))
        self.assertIsNotNone(self.state.load()["task"])
        self.assertEqual(len(self.store.rows), 2)

    def test_unknown_selection_never_writes(self):
        self.robot.handle(self.event("昨天打车36"))
        self.robot.handle(self.event("昨天打车32"))
        self.robot.handle(self.event("昨天打车改成30"))
        self.assertIn("请选择", self.robot.handle(self.event("第九笔")))
        self.assertEqual(len(self.store.rows), 2)
