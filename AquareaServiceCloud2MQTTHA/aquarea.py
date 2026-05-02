
"""
Main Aquarea class — equivalent of aquarea.go
"""

import asyncio
import json
import logging
import re
import ssl
from pathlib import Path

import aiohttp

from aquarea_types import (
    AquareaCommand,
    AquareaEndUserJSON,
    AquareaFunctionDescription,
    AquareaFunctionSettingGetJSON,
    AquareaLogItem,
)
from aquarea_http import AquareaHTTPMixin
from aquarea_login import AquareaLoginMixin
from aquarea_settings import AquareaSettingsMixin
from aquarea_device_status import AquareaDeviceStatusMixin
from aquarea_device_statistics import AquareaDeviceStatisticsMixin
from mqtt_discovery import AquareaDiscoveryMixin

TRANSLATION_FILE = "translation.json"
logger = logging.getLogger(__name__)


class Aquarea(
    AquareaHTTPMixin,
    AquareaLoginMixin,
    AquareaSettingsMixin,
    AquareaDeviceStatusMixin,
    AquareaDeviceStatisticsMixin,
    AquareaDiscoveryMixin,
):
    def __init__(self):
        self.aquarea_service_cloud_url: str = ""
        self.aquarea_service_cloud_login: str = ""
        self.aquarea_service_cloud_password: str = ""
        self.log_sec_offset: int = 0

        self.data_queue: asyncio.Queue = None
        self.status_queue: asyncio.Queue = None
        self.session: aiohttp.ClientSession = None

        self.dictionary_web_ui: dict[str, str] = {}
        self.reverse_dictionary_web_ui: dict[str, str] = {}
        self.users_map: dict[str, AquareaEndUserJSON] = {}
        self.translation: dict[str, AquareaFunctionDescription] = {}
        self.reverse_translation: dict[str, str] = {}
        self.log_items: dict[str, list[AquareaLogItem]] = {}  # gwid → items
        self._placeholder_ranges_applied: set[str] = set()  # gwid set
        self._log_labels_2903: dict[str, str] = {}
        self.aquarea_settings: AquareaFunctionSettingGetJSON = AquareaFunctionSettingGetJSON()
        self._shiesuahruefutohkun: str = ""

    def load_translations(self, filename: str):
        raw: dict = json.loads(Path(filename).read_text(encoding="utf-8"))
        self.translation = {
            key: AquareaFunctionDescription.from_dict(val)
            for key, val in raw.items()
        }
        self.reverse_translation = {
            descr.name: key
            for key, descr in self.translation.items()
            if "setting-user-select" in key
        }

    async def fetch_token_from_installer_state(self) -> str:
        """Fetch fresh token from installerState — only call after login."""
        home_url = self.aquarea_service_cloud_url + "installer/home"
        installer_state_url = self.aquarea_service_cloud_url + "page/api/installerState"
        body = await self.http_get_with_referer(installer_state_url, home_url)
        data = json.loads(body)
        token = data.get("shiesuahruefutohkun")
        if not token:
            raise ValueError(f"No shiesuahruefutohkun in installerState: {data}")
        self._shiesuahruefutohkun = token
        return token

    async def get_shiesuahruefutohkun(self, url: str = None) -> str:
        """Return cached token, fetching if needed."""
        if self._shiesuahruefutohkun:
            return self._shiesuahruefutohkun
        return await self.fetch_token_from_installer_state()

    async def get_end_user_shiesuahruefutohkun(self, user: AquareaEndUserJSON) -> str:
        """Return cached token."""
        return await self.get_shiesuahruefutohkun()

    async def feed_data_from_aquarea(self):
        # Snapshot the user list before iterating: aquarea_setup() called on
        # token expiry repopulates users_map, which would raise RuntimeError
        # ("dictionary changed size during iteration") without this copy.
        for user in list(self.users_map.values()):
            try:
                shiesuahruefutohkun = await self.get_end_user_shiesuahruefutohkun(user)
            except Exception as e:
                await self.status_queue.put(False)
                logger.error("Failed to get token for device %s: %s", user.gwid, e)
                logger.info("Will attempt to log in again")
                self._shiesuahruefutohkun = ""
                await self.aquarea_setup()
                continue

            try:
                device_status = await self.parse_device_status(user, shiesuahruefutohkun)
                await self.data_queue.put(device_status)
            except Exception as e:
                logger.error("parse_device_status failed for device %s: %s", user.gwid, e)

            try:
                settings = await self.get_device_settings(user, shiesuahruefutohkun)
                await self.data_queue.put(settings)
            except Exception as e:
                logger.error("get_device_settings failed for device %s: %s", user.gwid, e)

            try:
                log_data = await self.get_device_log_information(user, shiesuahruefutohkun)
                if log_data:
                    await self.data_queue.put(log_data)
            except Exception as e:
                logger.error("get_device_log_information failed for device %s: %s", user.gwid, e)

            await self.status_queue.put(True)


async def aquarea_handler(
    ctx: asyncio.Event,
    config: dict,
    data_queue: asyncio.Queue,
    command_queue: asyncio.Queue,
    status_queue: asyncio.Queue,
):
    logger.info("Starting Aquarea Service Cloud handler")

    aq = Aquarea()
    aq.aquarea_service_cloud_url = config["AquareaServiceCloudURL"]
    aq.aquarea_service_cloud_login = config["AquareaServiceCloudLogin"]
    aq.aquarea_service_cloud_password = config["AquareaServiceCloudPassword"]
    aq.log_sec_offset = config.get("LogSecOffset", 0)

    # Set API language — controls labels returned by Panasonic (sensors, settings)
    language = config.get("Language", "en")
    aq.set_language(language)
    logger.info("API language set to: %s", language)
    aq.data_queue = data_queue
    aq.status_queue = status_queue

    aq.load_translations(TRANSLATION_FILE)

    pool_interval = float(config["PoolInterval"])
    timeout_sec = float(config.get("AquareaTimeout", 30))

    ssl_verify = config.get("AquareaServiceCloudSSLVerify", True)
    if ssl_verify:
        # Default: full certificate verification (recommended)
        connector = aiohttp.TCPConnector()
    else:
        # Escape hatch for self-signed / intercepting proxies — set
        # AquareaServiceCloudSSLVerify: false in options.json to use.
        logger.warning(
            "SSL certificate verification is DISABLED "
            "(AquareaServiceCloudSSLVerify=false) — use only for debugging"
        )
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    aq.session = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=timeout_sec),
    )

    logger.info("Attempting to log in to Aquarea Service Cloud")
    while not await aq.aquarea_setup():
        await asyncio.sleep(5)
    logger.info("Logged in to Aquarea Service Cloud")

    async def poll_loop():
        loop = asyncio.get_event_loop()
        while not ctx.is_set():
            next_tick = loop.time() + pool_interval
            await aq.feed_data_from_aquarea()
            remaining = next_tick - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def command_loop():
        while not ctx.is_set():
            try:
                cmd: AquareaCommand = await asyncio.wait_for(
                    command_queue.get(), timeout=1.0
                )
                await aq.send_setting(cmd)
            except asyncio.TimeoutError:
                pass

    try:
        await asyncio.gather(poll_loop(), command_loop())
    finally:
        await aq.session.close()