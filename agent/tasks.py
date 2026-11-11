"""Validated selection tasks; never execute arbitrary tool names from text."""
import decimal
import re


def selection(text, candidates):
    match = re.fullmatch(r"(?:第)?([一二三四五六七八九十]|\d+)(?:笔|个|条)?", text.strip())
    if match:
        number = match[1]
        index = int(number) if number.isdigit() else "一二三四五六七八九十".index(number) + 1
        return candidates[index - 1] if 1 <= index <= len(candidates) else None
    match = re.fullmatch(r"(\d+(?:\.\d{1,2})?)(?:元)?(?:的)?(?:那笔)?", text.strip())
    if match:
        rows = [r for r in candidates if decimal.Decimal(str(r["金额"])) == decimal.Decimal(match[1])]
        return rows[0] if len(rows) == 1 else None
    return None


def prompt(task):
    lines = [f"没有唯一确定目标，尚未修改账单。找到多笔{task['label']}，请选择要{task['action']}的一笔："]
    lines += [f"{i}. {r['日期']} {r['项目']} ¥{r['金额']}" for i, r in enumerate(task["options"], 1)]
    return "\n".join(lines) + "\n回复“第二笔”“36元的”，或“取消这个操作”。"
