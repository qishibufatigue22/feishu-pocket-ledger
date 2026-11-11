"""Exact financial aggregation; no language-model arithmetic."""
import datetime as dt
import decimal

class AnalyticsTools:
    def spending_reply(self, query):
        totals, days, count = {}, {}, 0
        for year in range(query.start.year, query.end.year + 1):
            token = self.store.config["years"].get(str(year))
            if not token:
                continue
            for row in self.store.records(
                token, "消费明细",
                [["状态", "==", "已入账"], ["发送者", "==", self.owner]],
            ):
                date = dt.date.fromisoformat(row["日期"])
                category = row["分类"]
                if not query.start <= date <= query.end or (query.category and category != query.category):
                    continue
                amount = decimal.Decimal(str(row["金额"]))
                totals[category] = totals.get(category, 0) + amount
                days[date] = days.get(date, 0) + amount
                count += 1
        label = query.category or "支出"
        result = f"{query.start} 至 {query.end}，{label}合计 ¥{sum(totals.values()):.2f}（{count}笔已入账）。"
        if query.ranking and totals:
            ranked = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
            result += "\n" + "\n".join(f"{i}. {key} ¥{value:.2f}" for i, (key, value) in enumerate(ranked, 1))
        if query.peak and days:
            peak = max(days.values())
            dates = "、".join(str(day) for day in sorted(days) if days[day] == peak)
            result += f"\n支出最高：{dates}，¥{peak:.2f}。"
        if (query.start.year, query.start.month) == (query.end.year, query.end.month):
            url = self.store.get_bill_link(query.start.year, query.start.month)
            if url:
                result += f"\n账单：{url}"
        return result
