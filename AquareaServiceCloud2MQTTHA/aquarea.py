"""
Main Aquarea class — equivalent of aquarea.go
"""

import asyncio
import hashlib
import json
import logging
import re
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from aquarea_types import (
    AquareaCommand,
    AquareaEndUserJSON,
    AquareaFunctionDescription,
    AquareaFunctionSettingGetJSON,
    AquareaLogItem,
)
from aquarea_http import AquareaHTTPMixin, AquareaSessionError
from aquarea_login import AquareaLoginMixin
from aquarea_settings import AquareaSettingsMixin
from aquarea_device_status import AquareaDeviceStatusMixin
from aquarea_device_statistics import AquareaDeviceStatisticsMixin
from mqtt_discovery import AquareaDiscoveryMixin

TRANSLATION_FILE = "translation.json"

# Minimum delay between two session rebuilds, to avoid hammering the login
# endpoint when Panasonic itself is down / in maintenance.
MIN_RESET_INTERVAL_SEC = 300
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

        # HTTP session factory parameters (set by aquarea_handler)
        self.ssl_verify: bool = True
        self.timeout_sec: float = 30

        # Stale-data watchdog
        self.stale_timeout_sec: float = 1800      # 0 = disabled
        self.last_log_key: dict[str, str] = {}    # gwid → last logData timestamp key
        self._fresh_sig: dict[str, tuple] = {}    # gwid → (status hash, log key)
        self._fresh_since: dict[str, float] = {}  # gwid → monotonic time of last change
        self._last_reset: float = 0.0
        self.session_resets: int = 0
        self._diag_discovered: set[str] = set()

    # ------------------------------------------------------------------
    # Diagnostic entities (for HA notifications)
    # ------------------------------------------------------------------
    def _diag_discovery(self, gwid: str) -> dict[str, str]:
        dev = {"identifiers": gwid, "manufacturer": "Panasonic",
               "model": "Aquarea", "name": f"Aquarea {gwid}"}
        base = f"aquarea/{gwid}/diag"
        out: dict[str, str] = {}

        def add(component: str, oid: str, cfg: dict):
            cfg.update({"unique_id": f"{gwid}_diag_{oid}", "device": dev,
                        "entity_category": "diagnostic"})
            out[f"homeassistant/{component}/{gwid}/diag_{oid}/config"] = json.dumps(cfg)

        # Bridge process alive (LWT) — no availability: must show OFF when dead
        add("binary_sensor", "addon", {
            "name": "Add-on", "device_class": "connectivity",
            "state_topic": "aquarea/status",
            "payload_on": "online", "payload_off": "offline",
        })
        # Panasonic cloud session healthy
        add("binary_sensor", "cloud", {
            "name": "Panasonic Cloud", "device_class": "connectivity",
            "state_topic": f"{base}/cloud", "payload_on": "ON", "payload_off": "OFF",
        })
        add("sensor", "session_resets", {
            "name": "Session resets", "state_topic": f"{base}/session_resets",
            "icon": "mdi:restart",
        })
        add("sensor", "last_reset", {
            "name": "Last session reset", "device_class": "timestamp",
            "state_topic": f"{base}/last_reset",
        })
        add("sensor", "last_reset_reason", {
            "name": "Last session reset reason",
            "state_topic": f"{base}/last_reset_reason", "icon": "mdi:alert-circle-outline",
        })
        return out

    async def publish_cloud_state(self, gwid: str, ok: bool) -> None:
        msg: dict[str, str] = {}
        if gwid not in self._diag_discovered:
            msg.update(self._diag_discovery(gwid))
            msg[f"aquarea/{gwid}/diag/session_resets"] = str(self.session_resets)
            self._diag_discovered.add(gwid)
        msg[f"aquarea/{gwid}/diag/cloud"] = "ON" if ok else "OFF"
        await self.data_queue.put(msg)

    async def publish_reset(self, reason: str) -> None:
        self.session_resets += 1
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for gwid in list(self.users_map):
            await self.data_queue.put({
                f"aquarea/{gwid}/diag/session_resets": str(self.session_resets),
                f"aquarea/{gwid}/diag/last_reset": now_iso,
                # HA sensor state is limited to 255 chars
                f"aquarea/{gwid}/diag/last_reset_reason": reason[:250],
            })

    def new_http_session(self) -> aiohttp.ClientSession:
        """Fresh ClientSession = fresh cookie jar + fresh connection pool.
        This is exactly what an add-on restart gives us."""
        if self.ssl_verify:
            connector = aiohttp.TCPConnector()
        else:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        return aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
        )

    async def reset_session(self, reason: str) -> bool:
        """Drop cookies + token and log in again from scratch."""
        now = time.monotonic()
        if now - self._last_reset < MIN_RESET_INTERVAL_SEC:
            logger.debug("Session reset skipped (last one %.0fs ago): %s", now - self._last_reset, reason)
            return False
        self._last_reset = now

        logger.warning("Rebuilding Panasonic session: %s", reason)
        await self.status_queue.put(False)
        await self.publish_reset(reason)
        for gwid in list(self.users_map):
            await self.publish_cloud_state(gwid, False)
        old = self.session
        self.session = self.new_http_session()
        self._shiesuahruefutohkun = ""
        try:
            await old.close()
        except Exception:
            pass

        ok = await self.aquarea_setup()
        # Restart the stale clocks whatever the outcome: next reset at the
        # earliest after another full stale_timeout_sec.
        self._fresh_sig.clear()
        self._fresh_since.clear()
        if ok:
            logger.info("Panasonic session rebuilt successfully")
        else:
            logger.error("Panasonic re-login failed, will retry on a later poll")
        return ok

    def _track_freshness(self, gwid: str, status: dict | None) -> float:
        """Return how long (s) neither the status payload nor the log
        timestamp of this device has changed."""
        status_hash = (
            hashlib.md5(json.dumps(status, sort_keys=True).encode()).hexdigest()
            if status else None
        )
        sig = (status_hash, self.last_log_key.get(gwid))
        now = time.monotonic()
        if self._fresh_sig.get(gwid) != sig:
            self._fresh_sig[gwid] = sig
            self._fresh_since[gwid] = now
            return 0.0
        return now - self._fresh_since.get(gwid, now)

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
        # Snapshot the user list before iterating: reset_session() repopulates
        # users_map, which would raise RuntimeError
        # ("dictionary changed size during iteration") without this copy.
        session_error: str | None = None
        stale: list[str] = []

        for user in list(self.users_map.values()):
            try:
                shiesuahruefutohkun = await self.get_end_user_shiesuahruefutohkun(user)
            except Exception as e:
                session_error = f"token fetch failed for {user.gwid}: {e}"
                break

            device_status = None
            ok = True
            try:
                device_status = await self.parse_device_status(user, shiesuahruefutohkun)
                await self.data_queue.put(device_status)
            except AquareaSessionError as e:
                session_error = f"status {user.gwid}: {e}"
                ok = False
            except Exception as e:
                ok = False
                logger.error("parse_device_status failed for device %s: %s", user.gwid, e)

            try:
                settings = await self.get_device_settings(user, shiesuahruefutohkun)
                await self.data_queue.put(settings)
            except AquareaSessionError as e:
                session_error = session_error or f"settings {user.gwid}: {e}"
                ok = False
            except Exception as e:
                ok = False
                logger.error("get_device_settings failed for device %s: %s", user.gwid, e)

            try:
                log_data = await self.get_device_log_information(user, shiesuahruefutohkun)
                if log_data:
                    await self.data_queue.put(log_data)
            except Exception as e:
                logger.error("get_device_log_information failed for device %s: %s", user.gwid, e)

            await self.status_queue.put(ok)

            is_stale = False
            if self.stale_timeout_sec > 0:
                frozen_for = self._track_freshness(user.gwid, device_status)
                if frozen_for >= self.stale_timeout_sec:
                    is_stale = True
                    stale.append(
                        f"{user.gwid} data unchanged for {frozen_for:.0f}s "
                        f"(log timestamp key={self.last_log_key.get(user.gwid)})"
                    )
            await self.publish_cloud_state(user.gwid, ok and not is_stale)

        if session_error:
            await self.reset_session(session_error)
        elif stale:
            await self.reset_session("stale data: " + "; ".join(stale))


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

    aq.ssl_verify = config.get("AquareaServiceCloudSSLVerify", True)
    aq.timeout_sec = timeout_sec
    aq.stale_timeout_sec = float(config.get("StaleDataTimeout", 1800))
    if not aq.ssl_verify:
        logger.warning(
            "SSL certificate verification is DISABLED "
            "(AquareaServiceCloudSSLVerify=false) — use only for debugging"
        )
    if aq.stale_timeout_sec > 0:
        logger.info("Stale-data watchdog: session rebuilt after %.0fs without any change", aq.stale_timeout_sec)
    else:
        logger.info("Stale-data watchdog disabled (StaleDataTimeout=0)")

    aq.session = aq.new_http_session()

    logger.info("Attempting to log in to Aquarea Service Cloud")
    while not await aq.aquarea_setup():
        await asyncio.sleep(5)
    logger.info("Logged in to Aquarea Service Cloud")

    async def poll_loop():
        loop = asyncio.get_event_loop()
        while not ctx.is_set():
            next_tick = loop.time() + pool_interval
            try:
                await aq.feed_data_from_aquarea()
            except Exception as e:
                # Never let the poll loop die silently
                logger.exception("Unexpected error in poll loop: %s", e)
            remaining = next_tick - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def command_loop():
        while not ctx.is_set():
            try:
                cmd: AquareaCommand = await asyncio.wait_for(
                    command_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                await aq.send_setting(cmd)
            except Exception as e:
                logger.error("send_setting failed for %s: %s", cmd, e)

    try:
        await asyncio.gather(poll_loop(), command_loop())
    finally:
        await aq.session.close()