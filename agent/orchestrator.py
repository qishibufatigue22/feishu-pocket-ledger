"""Rule-based agent: observe, select, clarify, execute and verify."""
import datetime as dt
import re
from knowledge.categories import CATEGORIES, ALIASES, TZ
from agent.intents import extract_category, parse_expense
from agent.tasks import selection, prompt
from storage.state import MemoryState
from tools.ledger import LedgerTools


class Robot(LedgerTools):
    def __init__(self, store, owner, state=None):
        super().__init__(store, owner)
        self.state = state if state is not None else MemoryState()

    def wait_for_selection(self, event, date, label, candidates, action, amount=None):
        data = self.state.load()
        data["task"] = {
            "kind": "selection", "status": "waiting", "action": action,
            "date": str(date), "label": label, "amount": amount,
            "options": candidates, "source_message": event["message_id"],
            "create_time": event["create_time"],
        }
        self.state.save(data)
        return prompt(data["task"])

    def handle(self, event, *, routed=False, internal=False):
        if (event.get("sender_id") != self.owner or event.get("sender_type") != "user"
                or event.get("chat_type") != "p2p" or event.get("message_type") != "text"):
            return None
        if not event.get("message_id") or self.store.is_completed(event["message_id"]):
            return None
        if internal or routed:
            return super().handle(event, routed=routed)
        event = dict(event)
        event.pop("_selected_id", None)
        text = event["content"].strip()
        data = self.state.load()
        task = data.get("task")
        if task:
            if text in ("取消", "取消这个操作", "不用了"):
                data["task"] = None
                self.state.save(data)
                return "已取消当前操作，没有修改账单。"
            if task["kind"] == "selection" and task.get("status") == "executing":
                result = super().handle(task["execution_event"])
                if result and result.startswith(("已修改", "已撤销", "这条消息已处理")):
                    data["task"] = None
                    self.state.save(data)
                return result
            if task["kind"] == "selection":
                chosen = selection(text, task["options"])
                if not chosen:
                    if re.search(r"改成|改为|新建|更新|撤销", text):
                        data["task"] = None
                        self.state.save(data)
                        return self.handle(event)
                    return prompt(task)
                date = dt.date.fromisoformat(task["date"])
                _, current = self.store.correction_candidates(date, self.owner)
                fresh = next((r for r in current if r["record_id"] == chosen["record_id"]), None)
                if not fresh or any(fresh.get(k) != chosen.get(k) for k in ("金额", "项目", "分类")):
                    data["task"] = None
                    self.state.save(data)
                    return "这笔记录已发生变化，当前操作已取消。请重新描述要处理的消费。"
                if task["action"] == "更正":
                    command = f"{date} {chosen['项目']}改成{task['amount']}"
                else:
                    command = f"撤销{date} {chosen['项目']}{chosen['金额']}"
                resumed = dict(event, content=command, create_time=task["create_time"],
                               _selected_id=chosen["record_id"])
                data["task"]["status"] = "executing"
                data["task"]["execution_event"] = resumed
                self.state.save(data)
                result = super().handle(resumed)
                if result and result.startswith(("已修改", "已撤销", "这条消息已处理")):
                    data["task"] = None
                    self.state.save(data)
                return result
            if task["kind"] == "remember":
                if text in ("记住", "是", "好的", "确认"):
                    data.setdefault("rules", {})[task["label"].casefold()] = task["category"]
                    data["task"] = None
                    self.state.save(data)
                    return f"已记住：{task['label']} → {task['category']}。以后同名项目会优先使用此分类。"
                if text in ("不用记住", "不要记住", "否"):
                    data["task"] = None
                    self.state.save(data)
                    return "不保存分类规则，刚才的入账结果保留。"
                # An unrelated request dismisses an optional learning prompt.
                data["task"] = None
                self.state.save(data)
        if text.startswith("忘记分类规则"):
            label = text[len("忘记分类规则"):].strip()
            removed = data.setdefault("rules", {}).pop(label.casefold(), None)
            self.state.save(data)
            return f"已删除{label}的分类规则。" if removed else "没有找到该分类规则。"
        pending = self.store.pending(self.owner)
        today = dt.datetime.fromtimestamp(int(event["create_time"]) / 1000, TZ).date()
        learned_event = event
        try:
            date, label, cents, category = parse_expense(text, today)
            rule = data.get("rules", {}).get(label.casefold())
            _, explicit = extract_category(text)
            if rule and not explicit:
                learned_event = dict(event, content=f"{date} {label}{cents / 100:.2f}（{rule}）")
        except ValueError:
            pass
        result = super().handle(learned_event)
        if pending and result and result.startswith("已记账"):
            row = pending[0][1]
            category = ALIASES.get(text, text)
            if text.isdigit() and 1 <= int(text) <= len(CATEGORIES):
                category = list(CATEGORIES)[int(text) - 1]
            if category in CATEGORIES:
                data["task"] = {"kind": "remember", "label": row["项目"], "category": category}
                self.state.save(data)
                result += f"\n要记住“{row['项目']} → {category}”吗？回复“记住”或“不用记住”。"
        return result
