"""
Device statistics / log — equivalent of aquareaDeviceStatistics.go
"""

import json
import logging
import math
import time
from datetime import datetime, timezone

from aquarea_types import AquareaEndUserJSON, AquareaLogDataJSON

logger = logging.getLogger(__name__)


# Panasonic log value meaning "no sensor / not applicable" for temperatures
# (pool, solar, buffer tank, zone water temps on installations without them).
_TEMP_SENTINEL = -78
_TEMP_UNITS = ("°C", "℃", "C")


def _format_val(val) -> str:
    """Format a numeric value avoiding float precision artifacts.

    logData values arrive as JSON strings ("11.600000000000001") as well as
    numbers, so strings are parsed too — otherwise they were published raw.
    """
    if isinstance(val, str):
        s = val.strip()
        try:
            f = float(s)
        except ValueError:
            return val
        if "." not in s and "e" not in s.lower():
            return s  # integer-like string: keep as is
        val = f
    if isinstance(val, float):
        if not math.isfinite(val):
            return str(val)
        if val == int(val):
            return str(int(val))
        return f"{val:.2f}".rstrip('0').rstrip('.')
    return str(val)


def _is_temp_sentinel(str_val: str, unit: str) -> bool:
    if not unit or not unit.endswith(_TEMP_UNITS):
        return False
    try:
        return float(str_val) == _TEMP_SENTINEL
    except ValueError:
        return False


class AquareaDeviceStatisticsMixin:

    async def get_device_log_information(
        self, user: AquareaEndUserJSON, shiesuahruefutohkun: str
    ) -> dict[str, str] | None:

        device_log_items = self.log_items.get(user.gwid, [])
        n = len(device_log_items)
        if n:
            value_list = json.dumps({"logItems": list(range(n))})
        else:
            value_list = '{"logItems":[]}'

        start_date = int(time.time()) - self.log_sec_offset

        b = await self.http_post(
            self.aquarea_service_cloud_url + "installer/api/data/log",
            {
                "var.deviceId": user.device_id,
                "shiesuahruefutohkun": shiesuahruefutohkun,
                "var.target": "0",
                "var.startDate": f"{start_date}000",
                "var.logItems": value_list,
            },
        )

        raw = json.loads(b)
        log_data = AquareaLogDataJSON.from_dict(raw)
        if not log_data.log_data:
            logger.warning(
                "data/log returned no logData for device %s (errorCode=%s)",
                user.gwid, log_data.error_code,
            )
            return None

        device_log: dict[str, list[str]] = json.loads(log_data.log_data)
        if not device_log:
            return None

        last_key = max(device_log.keys(), key=lambda k: int(k))
        stats: dict[str, str] = {}

        for i, val in enumerate(device_log[last_key]):
            unit = ""
            if i < len(device_log_items):
                item = device_log_items[i]
                name = item.name
                unit = item.unit
                if unit:
                    stats[f"aquarea/{user.gwid}/log/{name}/unit"] = unit
                # Format float BEFORE looking up in values dict
                # (values.get converts to str, losing float type)
                str_val = item.values.get(str(val), _format_val(val))
            else:
                name = f"item{i:03d}"
                str_val = _format_val(val)

            state_topic = f"aquarea/{user.gwid}/log/{name}"
            # Sensors with a unit are discovered with a per-sensor availability
            # topic (availability_mode "all").  Published for every item so a
            # unit coming from translation.json (UNIT_MAP) is covered too.
            if _is_temp_sentinel(str_val, unit):
                stats[state_topic + "/availability"] = "offline"
                continue
            stats[state_topic + "/availability"] = "online"
            stats[state_topic] = str_val

        # Freshness marker consumed by the stale-data watchdog (aquarea.py)
        self.last_log_key[user.gwid] = last_key
        ts_sec = int(last_key) / 1000
        ts_iso = datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        stats[f"aquarea/{user.gwid}/log/Timestamp"] = ts_iso
        stats[f"aquarea/{user.gwid}/log/CurrentError"] = str(log_data.error_code)
        logger.info("Get new Panasonic log data for device %s", user.gwid)
        logger.debug(
            "Panasonic log data for device %s, timestamp %s (%d values): %s",
            user.gwid,
            last_key,
            len(stats),
            json.dumps(stats, ensure_ascii=False),
        )
        return stats