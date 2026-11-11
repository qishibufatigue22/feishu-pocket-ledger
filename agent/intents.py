import datetime as dt
import decimal
import re
from knowledge.categories import CATEGORIES, WORDS, ALIASES
from natural_intent import expense_parts, query_intent

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

def parse_removal(text, today):
    """Recognize duplicate/withdrawal requests before amount corrections."""
    if not re.search(r"重复|撤销|删除|取消.*(?:消费|账|一笔)", text):
        return None
    duplicate = "重复" in text
    cleaned = re.sub(r"^(?:不对|记错了?)[，,：:\s]*", "", text.strip())
    cleaned = re.sub(
        r"^(?:请|帮我|把|将|撤销|删除|取消|一笔|一条|这笔)+\s*", "", cleaned
    )
    cleaned = re.sub(
        r"(?:重复(?:记账|入账)?了?|撤销|删除|取消)(?:一笔|一条|这笔)?[。！!\s]*$",
        "",
        cleaned,
    ).strip()
    has_amount = bool(re.search(r"\d+(?:\.\d{1,2})?\s*(?:元|块)?$", cleaned))
    try:
        date, label, cents, _ = parse_expense(
            cleaned if has_amount else cleaned + "0.01", today
        )
    except ValueError:
        raise ValueError(
            "请注明要撤销的消费，例如“10月24 iCloud重复了”或“撤销10月24 iCloud28”。"
        ) from None
    return date, label, cents if has_amount else None, duplicate

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
