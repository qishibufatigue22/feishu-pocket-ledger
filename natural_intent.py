"""Local intent routing. Text never selects arbitrary commands or tools."""

import calendar
import datetime as dt
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SpendingQuery:
    start: dt.date
    end: dt.date
    category: str = ""
    ranking: bool = False
    peak: bool = False


PERIODS = "今天|昨天|前天|上周|本周|这周|上个月|上月|这个月|本月|今年"


def query_intent(text, today, categories):
    if not re.search(r"花了多少|花了多少钱|花多少钱|支出多少|消费多少|花得最多|哪天最多|支出排行|支出排名", text):
        return None
    dates = re.findall(r"(?:(\d{4})年)?(\d{1,2})月", text)
    periods = re.findall(PERIODS, text)
    if len(dates) + len(periods) != 1 or re.search(r"\d月\s*\d{1,2}|去年|去年同期|前\d+[天周月]", text):
        raise ValueError("请注明一个统计时段，例如“上周吃饭花了多少”或“10月支出排行”。")
    if dates:
        year, month = int(dates[0][0] or today.year), int(dates[0][1])
        start = dt.date(year, month, 1)
        end = start.replace(day=calendar.monthrange(year, month)[1])
        period = dates[0][0] + ("年" if dates[0][0] else "") + dates[0][1] + "月"
    else:
        period = periods[0]
        if period in ("今天", "昨天", "前天"):
            start = end = today - dt.timedelta(days=("今天", "昨天", "前天").index(period))
        elif period in ("上周", "本周", "这周"):
            monday = today - dt.timedelta(days=today.weekday())
            start = monday - dt.timedelta(days=7) if period == "上周" else monday
            end = monday - dt.timedelta(days=1) if period == "上周" else today
        elif period in ("上月", "上个月"):
            end = today.replace(day=1) - dt.timedelta(days=1)
            start = end.replace(day=1)
        elif period == "今年":
            start, end = today.replace(month=1, day=1), today
        else:
            start, end = today.replace(day=1), today
    aliases = {**dict.fromkeys(categories), "吃饭": "伙食费", "餐饮": "伙食费"}
    found = {value or key for key, value in aliases.items() if key in text}
    if len(found) > 1:
        raise ValueError("请一次查询一个分类，或查询全部支出。")
    # Do not silently treat an item-specific question as a total.
    subject = text.replace(period, "", 1)
    for key in sorted(aliases, key=len, reverse=True):
        subject = subject.replace(key, "")
    subject = re.sub(
        r"帮我|请|看一下|查一下|统计一下|一共|总共|总计|全部|所有|各类|分类|花了多少钱|花了多少|花多少钱|支出多少|消费多少|花得最多的(?:三|3)类|花得最多|哪天最多|支出排行|支出排名|[，,。？?！!；;、\s]", "", subject
    )
    if subject:
        raise ValueError("我还不能确定要统计哪些消费。可以说“上周伙食费花了多少”或“这个月花得最多的三类”。")
    return SpendingQuery(start, end, next(iter(found), ""), bool(re.search(r"花得最多|排行|排名", text)), "哪天最多" in text)


def expense_parts(text):
    """Normalize explicitly requested entries, leaving legacy commands alone."""
    if re.search(r"别记|不要记|先不记|不记账|如果|假设", text):
        raise ValueError("这句话没有明确要求入账，未修改账单。")
    if re.search(r"撤销|删除|重复|取消|改成|改为|修改|更正", text):
        return None
    cleaned = re.sub(r"^(?:请)?(?:帮我|给我)(?:记一下|记一笔|记账|记下|记)[：:\s]*", "", text)
    cleaned = re.sub(r"[，,\s]*(?:请)?(?:帮我|给我)(?:记一下|记一笔|记账|记下|记)[。！!\s]*$", "", cleaned)
    if cleaned == text:
        return None
    parts = [part.strip() for part in re.split(r"[，,；;、\n]", cleaned.strip("。！! ")) if part.strip()]
    if not 1 <= len(parts) <= 10:
        raise ValueError("请一次发一到十笔消费，每笔注明项目和金额。")
    date = re.match(r"^(?:\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?|\d{1,2}月\d{1,2}日?|今天|昨天|前天)", parts[0])
    if date:
        for i in range(1, len(parts)):
            if not re.match(r"^(?:\d|今天|昨天|前天)", parts[i]):
                parts[i] = date[0] + " " + parts[i]
    return parts
