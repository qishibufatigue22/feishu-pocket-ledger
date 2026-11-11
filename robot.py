"""Compatibility entry point; agent implementation lives in agent/ and tools/."""
import logging
from knowledge.categories import CATEGORIES, TZ
from agent.intents import classify, parse_expense, parse_correction, parse_removal
from feishu_cli import CLI, CLIError, dumps, objects, find_id, decode_records
from storage.feishu import FeishuStore
from agent.orchestrator import Robot
from adapters.feishu_bot import run

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
