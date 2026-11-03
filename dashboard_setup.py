"""Provision native Feishu charts without copying or counting summary rows."""

import json
import re
from pathlib import Path

from feishu_cli import CLI, CLIError, dumps, find_id


def chart_config(group, month=None):
    conditions = [{"field_name": "状态", "operator": "is", "value": "已入账"}]
    if month:
        conditions.append(
            {"field_name": "日期", "operator": "contains", "value": month + "-"}
        )
    return {
        "table_name": "消费明细",
        "series": [{"field_name": "金额", "rollup": "SUM"}],
        "group_by": [
            {
                "field_name": group,
                "mode": "integrated",
                "sort": {"type": "group", "order": "asc"},
            }
        ],
        "filter": {"conjunction": "and", "conditions": conditions},
    }


def pages(cli, command, token, dashboard=None):
    items, cursor = [], None
    while True:
        args = ["base", command, "--base-token", token, "--page-size", "100"]
        if dashboard:
            args += ["--dashboard-id", dashboard]
        if cursor:
            args += ["--page-token", cursor]
        result = cli.call(*args)
        items.extend(
            result.get("items", result.get("blocks", result.get("dashboards", [])))
        )
        if not result.get("has_more"):
            return items
        cursor = result.get("page_token")
        if not cursor:
            raise CLIError("图表分页缺少游标。")


def setup(cli, token, year, verbose=False):
    tables = cli.call("base", "+table-list", "--base-token", token)["tables"]
    months = sorted(
        int(m[1]) for t in tables if (m := re.fullmatch(r"(\d{1,2})月账单", t["name"]))
    )
    fields = cli.call(
        "base", "+field-list", "--base-token", token, "--table-id", "消费明细"
    )["fields"]
    names = {f["name"] for f in fields}
    if not {"日期", "金额", "状态", "分类"} <= names:
        raise CLIError("消费明细缺少图表所需字段。")
    if "月份" not in names:
        cli.call(
            "base",
            "+field-create",
            "--base-token",
            token,
            "--table-id",
            "消费明细",
            "--json",
            dumps({"name": "月份", "type": "formula", "expression": "LEFT([日期],7)"}),
            "--i-have-read-guide",
        )
    name = f"{year}年支出分析"
    matches = [d for d in pages(cli, "+dashboard-list", token) if d.get("name") == name]
    if len(matches) > 1:
        raise CLIError("存在多个同名支出分析仪表盘。")
    created = not matches
    dashboard = find_id(
        (
            matches[0]
            if matches
            else cli.call(
                "base", "+dashboard-create", "--base-token", token, "--name", name
            )
        ),
        "dashboard_id",
        "id",
    )
    existing = {
        b["name"] for b in pages(cli, "+dashboard-block-list", token, dashboard)
    }
    planned = [
        (
            "数据来源与更新说明",
            "text",
            {
                "text": "## 数据来源与更新说明\n- 图表以「消费明细」为数据源，仅统计「已入账」消费，排除待分类、已取消和更正记录。\n- 通过机器人记账、更正，或修改「消费明细」后，图表会自动更新，可能有短暂延迟；未更新时可刷新。\n- 只手动修改月份汇总表不会改变图表，请优先通过机器人修改消费。"
            },
        ),
        ("全年各月支出对比", "column", chart_config("月份")),
    ]
    for month in months:
        prefix = f"{year}-{month:02d}"
        planned += [
            (f"{month}月分类支出占比", "pie", chart_config("分类", prefix)),
            (f"{month}月每日支出", "column", chart_config("日期", prefix)),
        ]
    for block_name, kind, data in planned:
        if block_name in existing:
            continue
        result = cli.call(
            "base",
            "+dashboard-block-create",
            "--base-token",
            token,
            "--dashboard-id",
            dashboard,
            "--name",
            block_name,
            "--type",
            kind,
            "--data-config",
            dumps(data),
        )
        if verbose:
            print(dumps({"name": block_name, "result": result}), flush=True)
    if created:
        cli.call(
            "base",
            "+dashboard-arrange",
            "--base-token",
            token,
            "--dashboard-id",
            dashboard,
        )
    return dashboard


if __name__ == "__main__":
    config = json.loads(Path(__file__).with_name("config.json").read_text())
    cli = CLI(config.get("profile"))
    for year, token in config["years"].items():
        dashboard = setup(cli, token, year, verbose=True)
        print(dumps({"year": year, "dashboard_id": dashboard}), flush=True)
