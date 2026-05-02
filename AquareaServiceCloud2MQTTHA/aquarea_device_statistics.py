"""
Device statistics / log — equivalent of aquareaDeviceStatistics.go
"""

import json
import logging
import time
from datetime import datetime, timezone

from aquarea_types import AquareaEndUserJSON, AquareaLogDataJSON

logger = logging.getLogger(__name__)


def _format_val(val: int | float) -> str:
    """Format a numeric value avoiding float precision artifacts."""
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    elif isinstance(val, float):
        return f"{val:.2f}".rstrip('0').rstrip('.')
    return str(val)


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
            return None

        device_log: dict[str, list[str]] = json.loads(log_data.log_data)
        if not device_log:
            return None

        last_key = max(device_log.keys(), key=lambda k: int(k))
        stats: dict[str, str] = {}

        for i, val in enumerate(device_log[last_key]):
            if i < len(device_log_items):
                item = device_log_items[i]
                name = item.name
                if item.unit:
                    stats[f"aquarea/{user.gwid}/log/{name}/unit"] = item.unit
                # Format float BEFORE looking up in values dict
                # (values.get converts to str, losing float type)
                str_val = item.values.get(str(val), _format_val(val))
            else:
                name = f"item{i:03d}"
                str_val = _format_val(val)

            stats[f"aquarea/{user.gwid}/log/{name}"] = str_val

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