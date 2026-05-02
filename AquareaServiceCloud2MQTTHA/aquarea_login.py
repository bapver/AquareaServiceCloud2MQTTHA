"""
Login & initialisation — equivalent of aquareaLogin.go
"""

import hashlib
import json
import logging
import re

from aquarea_types import (
    AquareaEndUserJSON,
    AquareaEndUsersListJSON,
    AquareaLoginJSON,
    AquareaLogItem,
)

logger = logging.getLogger(__name__)

_UNIT_RE = re.compile(r"(.+)\[(.+)\]")
_MULTI_CHOICE_RE = re.compile(r"(\d+)\s*:\s*([^,\]]+)")
_REMOVE_PARENS_RE = re.compile(r"\(.+?\)")


_SAFE_NAME_RE = re.compile(r'[^a-zA-Z0-9_-]')


def _transliterate(text: str) -> str:
    """Replace accented chars with ASCII equivalents before sanitizing."""
    import unicodedata
    normalized = unicodedata.normalize('NFKD', text)
    return normalized.encode('ascii', 'ignore').decode('ascii')


def _parse_log_label(raw_label: str) -> AquareaLogItem:
    label = raw_label.replace("(Actual)", "Actual").replace("(Target)", "Target")
    label = _REMOVE_PARENS_RE.sub("", label).strip()

    split = _UNIT_RE.search(label)
    if not split:
        # Transliterate accents before sanitizing (é->e, è->e, etc.)
        name = _transliterate(label).strip().title().replace(" ", "").replace(":", "")
        # Sanitize: remove chars that would break MQTT topic levels
        name = _SAFE_NAME_RE.sub("-", name).strip("-")
        return AquareaLogItem(name=name, unit="", values={})

    # Transliterate only the name part, preserve unit as-is (keep °C, ℃, etc.)
    name_raw = _transliterate(split.group(1)).strip().title().replace(" ", "").replace(":", "")
    # Sanitize: '/' and ',' create invalid MQTT topic levels
    name_raw = _SAFE_NAME_RE.sub("-", name_raw).strip("-")
    unit_part = split.group(2).strip()

    choices = _MULTI_CHOICE_RE.findall(unit_part)
    if choices:
        return AquareaLogItem(
            name=name_raw,
            unit="",
            values={m[0]: m[1].strip() for m in choices},
        )
    return AquareaLogItem(name=name_raw, unit=unit_part, values={})


class AquareaLoginMixin:

    async def aquarea_setup(self) -> bool:
        try:
            await self.aquarea_login()
        except Exception as e:
            logger.error("Login failed: %s", e)
            return False

        try:
            await self.aquarea_installer_home()
        except Exception as e:
            logger.error("Installer home failed: %s", e)
            return False

        await self.aquarea_initial_fetch()
        return True

    async def aquarea_initial_fetch(self):
        """First data fetch and Home Assistant discovery.

        Order matters:
          1. parse_device_status  — navigates to functionStatus, establishes
                                    device context in the server session.
          2. fetch_log_items      — needs valid session with selected device.
          3. get_device_settings  — also needs device context. Populates
                                    aquarea_settings with settingDataInfo and
                                    settingBackgroundData.
          4. fetch_placeholder_ranges — uses settingDataInfo/bgData to compute
                                    correct min/max/step via R()/D() statusNo logic.
          5. get_device_log_information — uses log_items built in step 2.
        """
        for user in self.users_map.values():
            try:
                shiesuahruefutohkun = await self.get_end_user_shiesuahruefutohkun(user)
            except Exception:
                continue

            # 1. Status — establishes device session context (init=True: fetches
            #    static endpoints that only need to be called once)
            try:
                status_data = await self.parse_device_status(user, shiesuahruefutohkun, init=True)
                if status_data:
                    ha_config_state = self.encode_sensors(status_data, user)
                    if ha_config_state:
                        await self.data_queue.put(ha_config_state)
                    await self.data_queue.put(status_data)
            except Exception as e:
                logger.error("Initial status fetch failed for device %s: %s", user.gwid, e)

            # 2. Log items schema — needs device context from step 1.
            #    Each device may have a different model with a different log schema,
            #    so we fetch and store it independently per gwid.
            if user.gwid not in self.log_items:
                try:
                    await self.fetch_log_items(user.gwid, shiesuahruefutohkun, self._log_labels_2903)
                except Exception as e:
                    logger.error("fetch_log_items failed for device %s: %s", user.gwid, e)

            # 3. Settings (also computes placeholder ranges internally)
            try:
                # Load option ranges from Panasonic JS on first call (once per process)
                await self.load_placeholder_options_from_js()
                settings = await self.get_device_settings(user, shiesuahruefutohkun)
                ha_config = self.encode_switches(settings, user)
                if ha_config:
                    await self.data_queue.put(ha_config)
            except Exception as e:
                logger.error("Initial settings fetch failed for device %s: %s", user.gwid, e)

            # 4. Logs
            try:
                log_data = await self.get_device_log_information(user, shiesuahruefutohkun)
                if log_data:
                    ha_config = self.encode_sensors(log_data, user)
                    if ha_config:
                        await self.data_queue.put(ha_config)
            except Exception as e:
                logger.error("Initial log fetch failed for device %s: %s", user.gwid, e)

    async def aquarea_login(self):
        import json as _json

        await self.http_get(self.aquarea_service_cloud_url + "page/api/settings")

        raw = (self.aquarea_service_cloud_login + self.aquarea_service_cloud_password).encode()
        password_md5 = hashlib.md5(raw).hexdigest()
        b = await self.http_post(
            self.aquarea_service_cloud_url + "installer/api/auth/login",
            {
                "var.loginId": self.aquarea_service_cloud_login,
                "var.password": password_md5,
                "var.inputOmit": "true",
                "shiesuahruefutohkun": "undefined",
            },
        )
        login = AquareaLoginJSON.from_dict(_json.loads(b))
        if login.error_code != 0:
            raise RuntimeError(f"Aquarea login error code: {login.error_code}")

        await self.http_get(self.aquarea_service_cloud_url + "installer/home")
        home_url = self.aquarea_service_cloud_url + "installer/home"
        installer_state_url = self.aquarea_service_cloud_url + "page/api/installerState"
        body = await self.http_get_with_referer(installer_state_url, home_url)
        data = _json.loads(body)
        token = data.get("shiesuahruefutohkun")
        if not token:
            raise ValueError(f"No shiesuahruefutohkun in installerState: {data}")
        self._shiesuahruefutohkun = token
        logger.info("Login OK, token: %s", token)

    async def aquarea_installer_home(self):
        shiesuahruefutohkun = self._shiesuahruefutohkun

        b = await self.http_post(
            self.aquarea_service_cloud_url + "installer/api/endusers",
            {
                "var.sortItem": "userName",
                "var.sortOrder": "0",
                "var.offset": "0",
                "var.limit": "92599",
                "var.readNew": "1",
                "shiesuahruefutohkun": shiesuahruefutohkun,
            },
        )
        end_users_list = AquareaEndUsersListJSON.from_dict(json.loads(b))
        for user in end_users_list.endusers:
            self.users_map[user.gwid] = user

        if not end_users_list.endusers:
            raise RuntimeError(
                "No Aquarea devices found on this account. "
                "Check that at least one heat pump is registered in Aquarea Service Cloud."
            )

        await self.get_dictionary(end_users_list.endusers[0])
        await self.status_queue.put(True)

    async def get_dictionary(self, user: AquareaEndUserJSON):
        """Fetch UI string translations via JSON APIs.
        Log items schema fetched later in aquarea_initial_fetch.
        """
        base = self.aquarea_service_cloud_url
        home_ref = base + "installer/home"

        # Static fallback for type 2010 — used if the API call fails.
        # Values sourced from the real Aquarea Service Cloud API (English).
        _DICT_2010_FALLBACK: dict[str, str] = {
            # Operation
            "2010-00D7": "Power: Off",
            "2010-00DC": "Power: On",
            # OperationMode
            "2010-00E1": "Tank",
            "2010-00E6": "Heat",
            "2010-00EB": "Cool",
            "2010-00F0": "Auto",
            "2010-00F5": "Heat + Tank",
            "2010-00FA": "Cool + Tank",
            "2010-00FF": "Auto + Tank",
            # ZoneOperationSetting
            "2010-0122": "Zone1: On, Zone2: Off",
            "2010-0127": "Zone1: Off, Zone2: On",
            "2010-012C": "Zone1: On, Zone2: On",
            # ForceDHW (0x01=Off, 0x02=On)
            "2010-0136": "Off",
            "2010-013B": "On",
            # WeeklyTimer (0x01=Off, 0x02=On)
            "2010-0140": "Off",
            "2010-0145": "On",
            # HolidayMode (0x01=Off, 0x02=On)
            "2010-014A": "Off",
            "2010-014F": "On",
            # QuietTimer (0x01=Off, 0x02=On)
            "2010-0168": "Off",
            "2010-016D": "On",
            # QuietMode
            "2010-0172": "Off",
            "2010-0177": "Level1",
            "2010-017C": "Level2",
            "2010-0181": "Level3",
            # Priority
            "2010-0182": "Priority: Sound",
            "2010-0183": "Priority: Capacity",
            # RoomHeater (0x01=Off, 0x02=On)
            "2010-0186": "Off",
            "2010-018B": "On",
            # TankHeater (0x01=Off, 0x02=On)
            "2010-0190": "Off",
            "2010-0195": "On",
            # TankSensor
            "2010-0197": "Top",
            "2010-0198": "Center",
            # Sterilization
            "2010-019A": "Request",
            # Powerful
            "2010-01A4": "Off",
            "2010-01A9": "On 30 mins",
            "2010-01AE": "On 60 mins",
            "2010-01B3": "On 90 mins",
            # ForceHeater (0x01=Off, 0x02=On)
            "2010-01B8": "Off",
            "2010-01BD": "On",
            # ForceDefrost
            "2010-01C2": "Request",
        }
        self.dictionary_web_ui.update(_DICT_2010_FALLBACK)

        # Fetch all types including 2010 — API returns English when account language is set.
        # Type 2010 will override the static fallback above if returned successfully.
        for type_code in ["2000", "2006", "2999", "2010"]:
            try:
                body = await self.http_get_with_referer(
                    base + f"page/api/text?var.types=%5B%22{type_code}%22%5D",
                    home_ref,
                )
                data = json.loads(body)
                if data.get("errorCode", -1) == 0:
                    received = data.get("text", {})
                    # For type 2010: only accept entries starting with "2010-"
                    # to avoid polluting the dict with unrelated codes
                    if type_code == "2010":
                        only_2010 = {k: v for k, v in received.items() if k.startswith("2010-")}
                        if only_2010:
                            self.dictionary_web_ui.update(only_2010)
                            logger.info(
                                "Loaded %d live type-2010 entries from API (overrides fallback)",
                                len(only_2010),
                            )
                        else:
                            logger.debug("Type 2010 API returned no 2010-* entries, using fallback")
                    else:
                        self.dictionary_web_ui.update(received)
            except Exception as e:
                logger.warning("Failed to fetch UI dictionary type %s: %s", type_code, e)

        self._log_labels_2903: dict[str, str] = {}
        try:
            body = await self.http_get_with_referer(
                base + "page/api/text?var.types=%5B%222903%22%5D",
                home_ref,
            )
            data = json.loads(body)
            if data.get("errorCode", -1) == 0:
                self._log_labels_2903 = data.get("text", {})
                self.dictionary_web_ui.update(self._log_labels_2903)
                logger.info("Panasonic loading dictionary (List available in log debug)")
                logger.debug(
                    "Panasonic log dictionary (2903): %d entries received — %s",
                    len(self._log_labels_2903),
                    json.dumps(self._log_labels_2903, ensure_ascii=False),
                )
        except Exception as e:
            logger.warning("Failed to fetch log dictionary (2903): %s", e)

        self.reverse_dictionary_web_ui = {v: k for k, v in self.dictionary_web_ui.items()}

    async def fetch_log_items(self, gwid: str, token: str, log_labels_2903: dict[str, str]) -> None:
        """
        Appelle /page/api/functionStatistics pour obtenir la liste ordonnée
        des clés 2903-xxxx. Position i dans la liste = indice i dans logData.
        Doit être appelé APRÈS parse_device_status (contexte device requis).
        Stocke le résultat dans self.log_items[gwid] — chaque device a son propre
        schéma de log car les modèles peuvent différer.
        """
        base = self.aquarea_service_cloud_url
        ref = base + "installer/functionStatus"

        body = await self.http_get_with_referer(
            base + f"page/api/functionStatistics?shiesuahruefutohkun={token}",
            ref,
        )
        data = json.loads(body)

        if data.get("errorCode", -1) != 0:
            raise RuntimeError(f"fetch_log_items errorCode={data.get('errorCode')}")

        raw_items = data.get("logItems", "[]")
        ordered_keys: list[str] = json.loads(raw_items) if isinstance(raw_items, str) else raw_items

        # Build log_items with deduplication: if two API entries produce the same
        # sanitized name, the second gets a _2 suffix, the third _3, etc.
        seen_names: dict[str, int] = {}
        deduped: list[AquareaLogItem] = []
        for k in ordered_keys:
            item = _parse_log_label(log_labels_2903.get(k, k))
            base_name = item.name
            count = seen_names.get(base_name, 0) + 1
            seen_names[base_name] = count
            if count > 1:
                item = AquareaLogItem(name=f"{base_name}_{count}", unit=item.unit, values=item.values)
                logger.debug("Duplicate log item name '%s' → renamed to '%s'", base_name, item.name)
            deduped.append(item)

        self.log_items[gwid] = deduped
        logger.info("Panasonic loading schema for device %s (%d items)", gwid, len(deduped))
        logger.debug(
            "Panasonic log schema (functionStatistics) device %s: %s",
            gwid,
            ", ".join(
                f"{item.name}{'['+item.unit+']' if item.unit else ''}"
                for item in deduped
            ),
        )