"""Append date-only project requirements and changes to the configured Feishu doc."""
import argparse
import datetime as dt
import hashlib
import html
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from feishu_cli import CLI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirement", action="append", required=True)
    parser.add_argument("--change", action="append", required=True)
    args = parser.parse_args()
    settings = json.loads((ROOT / "project_log.local.json").read_text())
    date = str(dt.datetime.now(ZoneInfo("Asia/Shanghai")).date())
    identifier = hashlib.sha256(json.dumps([date, args.requirement, args.change], ensure_ascii=False).encode()).hexdigest()[:12]
    marker = "记录编号：" + identifier
    cli = CLI()
    doc = settings["document_url"]
    before = cli.call("docs", "+fetch", "--doc", doc, "--detail", "full")["document"]["content"]
    if marker in before:
        print("此记录已存在，未重复追加。")
        return
    def items(values):
        return "<ul>" + "".join("<li>" + html.escape(text) + "</li>" for text in values) + "</ul>"
    content = "<h2>" + date + "</h2><h3>需求记录</h3>" + items(args.requirement)
    content += "<h3>更改记录</h3>" + items(args.change) + "<p>" + marker + "</p>"
    result = cli.call("docs", "+update", "--doc", doc, "--command", "append", "--content", content)
    if result.get("result") != "success" or result.get("warnings"):
        raise RuntimeError("文档写入未完全成功，请检查返回状态；重试前先读取文档。")
    after = cli.call("docs", "+fetch", "--doc", doc, "--scope", "keyword", "--keyword", marker)["document"]["content"]
    if marker not in after:
        raise RuntimeError("文档追加已提交，但未能读回确认；请先核对文档。")
    print("已追加并验证：" + date + "，" + marker)


if __name__ == "__main__":
    main()
