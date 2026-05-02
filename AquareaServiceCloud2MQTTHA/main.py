"""
Entry point — equivalent of main.go
"""

# Must be before any other import that touches asyncio internals.
# aiomqtt/paho-mqtt use add_reader/add_writer which only work with
# SelectorEventLoop, not the default IocpProactor on Windows.
import sys
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncio
import json
import logging
import os
import platform
import signal
from pathlib import Path

from aquarea import aquarea_handler
from mqtt import mqtt_handler

CONFIG_FILE_OTHER = "/data/options.json"
CONFIG_FILE_WINDOWS = "options.json"

logging.basicConfig(
    level=logging.INFO,  # updated after config load below
    format="%(asctime)s %(levelname)s %(filename)s:%(lineno)d %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def read_config() -> dict:
    config_file = (
        CONFIG_FILE_WINDOWS if platform.system() == "Windows" else CONFIG_FILE_OTHER
    )
    return json.loads(Path(config_file).read_text(encoding="utf-8"))


async def main():
    config = read_config()
    log_level = getattr(logging, config.get("LogLevel", "INFO").upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)

    data_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    command_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    status_queue: asyncio.Queue = asyncio.Queue()

    stop_event = asyncio.Event()

    mqtt_task = asyncio.create_task(
        mqtt_handler(stop_event, config, data_queue, command_queue, status_queue)
    )
    aquarea_task = asyncio.create_task(
        aquarea_handler(stop_event, config, data_queue, command_queue, status_queue)
    )

    logger.info("Running — press Ctrl+C to stop")
    try:
        await asyncio.gather(mqtt_task, aquarea_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop_event.set()
        logger.info("Shut down complete")


if __name__ == "__main__":
    asyncio.run(main())