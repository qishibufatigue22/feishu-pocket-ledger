"""Start the Feishu financial agent."""
import logging
from adapters.feishu_bot import run

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
