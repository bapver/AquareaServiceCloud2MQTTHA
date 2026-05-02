"""
MQTT Home Assistant discovery — equivalent of mqttDiscovery.go

Settings discovery:
  - single "Request" option           → button HA
  - 1-2 options with On/Off/Request   → switch HA
  - 3+ options, or 2 without On/Off   → select HA
"""

import json
import logging
import re
import os
from dataclasses import dataclass, field, asdict
from aquarea_types import AquareaEndUserJSON

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unit map from translation.json
# ---------------------------------------------------------------------------
_TRANSLATION_PATH = os.path.join(os.path.dirname(__file__), "translation.json")

def _load_unit_map(path: str = _TRANSLATION_PATH) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    return {
        v["name"]: v["unit"]
        for v in data.values()
        if "name" in v and "unit" in v
    }

UNIT_MAP: dict[str, str] = _load_unit_map()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class _Device:
    manufacturer: str = ""
    model: str = ""
    name: str = ""
    identifiers: str = ""

def _panasonic(device_id: str) -> _Device:
    return _Device(
        manufacturer="Panasonic",
        model="Aquarea",
        identifiers=device_id,
        name=f"Aquarea {device_id}",
    )

def _clean(d: dict) -> dict:
    """Remove falsy fields, but keep lists (even empty)."""
    return {k: v for k, v in d.items() if v or isinstance(v, list)}

@dataclass
class MqttSwitch:
    name: str = ""
    availability_topic: str = ""
    command_topic: str = ""
    state_topic: str = ""
    payload_on: str = ""
    payload_off: str = ""
    unique_id: str = ""
    device: _Device = field(default_factory=_Device)

@dataclass
class MqttSelect:
    """HA MQTT select — for multi-option settings."""
    name: str = ""
    availability_topic: str = ""
    command_topic: str = ""
    state_topic: str = ""
    options: list = field(default_factory=list)
    unique_id: str = ""
    device: _Device = field(default_factory=_Device)

@dataclass
class MqttButton:
    """HA MQTT button — for one-shot actions (Request)."""
    name: str = ""
    availability_topic: str = ""
    command_topic: str = ""
    payload_press: str = ""
    unique_id: str = ""
    device: _Device = field(default_factory=_Device)

@dataclass
class MqttNumber:
    """HA MQTT number — for writable numeric settings (temperatures, shifts)."""
    name: str = ""
    availability_topic: str = ""
    state_topic: str = ""
    command_topic: str = ""
    unit_of_measurement: str = ""
    min: float = -128
    max: float = 127
    step: float = 1
    unique_id: str = ""
    device: _Device = field(default_factory=_Device)

@dataclass
class MqttSensor:
    name: str = ""
    availability_topic: str = ""
    state_topic: str = ""
    unit_of_measurement: str = ""
    device_class: str = ""
    state_class: str = ""
    force_update: bool = False
    unique_id: str = ""
    device: _Device = field(default_factory=_Device)

    # Per-sensor availability topic (set when sensor can be unavailable)
    availability: list = field(default_factory=list)

@dataclass
class MqttBinarySensor:
    name: str = ""
    availability_topic: str = ""
    state_topic: str = ""
    device_class: str = ""
    force_update: bool = False
    payload_off: str = ""
    payload_on: str = ""
    unique_id: str = ""
    device: _Device = field(default_factory=_Device)


def _to_json(obj) -> str:
    d = asdict(obj)
    d["device"] = _clean(d["device"])
    # If availability list is set, remove single availability_topic (list takes precedence)
    if d.get("availability"):
        d.pop("availability_topic", None)
    else:
        d.pop("availability", None)
    return json.dumps(_clean(d))


def _slugify(name: str) -> str:
    """Convert a label to a valid MQTT topic segment.

    HA Discovery requires topic levels to contain only:
      a-z, A-Z, 0-9, _ and -
    Steps:
      1. Transliterate accented chars to ASCII equivalents (é→e, ç→c …)
      2. Replace spaces and dots with underscores
      3. Strip every remaining non-allowed character
    """
    import unicodedata
    # NFKD decomposes accented chars (é → e + combining accent)
    normalized = unicodedata.normalize("NFKD", name)
    # encode to ASCII, ignoring the combining diacritics
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    # spaces and dots → underscore
    ascii_name = re.sub(r"[ .]", "_", ascii_name)
    # drop everything that's not a-z, A-Z, 0-9, _ or -
    return re.sub(r"[^a-zA-Z0-9_-]", "", ascii_name)


# ---------------------------------------------------------------------------
# Individual encoders
# ---------------------------------------------------------------------------

def encode_binary_sensor(name: str, device_id: str, state_topic: str) -> tuple[str, str]:
    safe = _slugify(name)
    s = MqttBinarySensor(
        name=name,
        availability_topic="aquarea/status",
        state_topic=state_topic,
        payload_on="On",
        payload_off="Off",
        unique_id=f"{device_id}_{safe}",
        device=_panasonic(device_id),
    )
    return "", _to_json(s)


def encode_sensor(name: str, device_id: str, state_topic: str, unit: str = "", device_class: str = "") -> tuple[str, str]:
    safe = _slugify(name)
    # For sensors with a unit, use a per-sensor availability topic in addition
    # to the global aquarea/status. This lets us mark individual sensors
    # unavailable (e.g. Zone1TemperatureSet when zone is disabled) without
    # sending the non-numeric string "unavailable" on the state_topic itself,
    # which HA rejects for numeric sensors.
    avail_topic = state_topic + "/availability"
    s = MqttSensor(
        name=name,
        availability_topic="aquarea/status",
        state_topic=state_topic,
        unit_of_measurement=unit,
        device_class=device_class,
        state_class="measurement" if unit else "",
        unique_id=f"{device_id}_{safe}",
        device=_panasonic(device_id),
        availability=[
            {"topic": "aquarea/status"},
            {"topic": avail_topic},
        ] if unit else [],
    )
    return avail_topic if unit else "", _to_json(s)


def encode_switch(name: str, device_id: str, state_topic: str, values: list[str], display_name: str = "") -> tuple[str, str]:
    """Binary On/Off switch. Raises ValueError if no On/Off/Request found."""
    safe = _slugify(name)
    b = MqttSwitch(
        name=display_name or name,
        availability_topic="aquarea/status",
        command_topic=state_topic + "/set",
        state_topic=state_topic,
        unique_id=f"{device_id}_{safe}",
        device=_panasonic(device_id),
    )
    found = False
    for v in values:
        vs = v.strip()
        if "Off" in vs:
            b.payload_off = vs
            found = True
        if "On" in vs:
            b.payload_on = vs
            found = True
        if "Request" in vs:
            b.payload_on = vs
            found = True
    if not found:
        raise ValueError(f"Cannot encode switch: {values}")
    ha_topic = f"homeassistant/switch/{device_id}/{safe}/config"
    return ha_topic, _to_json(b)


def encode_select(name: str, device_id: str, state_topic: str, options: list[str], display_name: str = "") -> tuple[str, str]:
    """Multi-option select."""
    safe = _slugify(name)
    s = MqttSelect(
        name=name,
        availability_topic="aquarea/status",
        command_topic=state_topic + "/set",
        state_topic=state_topic,
        options=[o.strip() for o in options if o.strip()],
        unique_id=f"{device_id}_{safe}",
        device=_panasonic(device_id),
    )
    ha_topic = f"homeassistant/select/{device_id}/{safe}/config"
    return ha_topic, _to_json(s)


def encode_button(name: str, device_id: str, state_topic: str, payload: str, display_name: str = "") -> tuple[str, str]:
    """One-shot button (Sterilization, ForceDefrost)."""
    safe = _slugify(name)
    b = MqttButton(
        name=name,
        availability_topic="aquarea/status",
        command_topic=state_topic + "/set",
        payload_press=payload,
        unique_id=f"{device_id}_{safe}",
        device=_panasonic(device_id),
    )
    ha_topic = f"homeassistant/button/{device_id}/{safe}/config"
    return ha_topic, _to_json(b)


def encode_number(name: str, device_id: str, state_topic: str, unit: str = "", display_name: str = "",
                  min_val: float = -128, max_val: float = 127, step: float = 1) -> tuple[str, str]:
    """Writable numeric setting (temperatures, shift values)."""
    safe = _slugify(name)
    # Include min/max in unique_id so HA recreates the entity if limits change
    unique_id = f"{device_id}_{safe}_{int(min_val)}_{int(max_val)}"
    n = MqttNumber(
        name=name,
        availability_topic="aquarea/status",
        state_topic=state_topic,
        command_topic=state_topic + "/set",
        unit_of_measurement=unit,
        min=min_val,
        max=max_val,
        step=step,
        unique_id=unique_id,
        device=_panasonic(device_id),
    )
    ha_topic = f"homeassistant/number/{device_id}/{safe}/config"
    return ha_topic, _to_json(n)


# ---------------------------------------------------------------------------
# Main mixin
# ---------------------------------------------------------------------------

class AquareaDiscoveryMixin:

    def encode_switches(self, topics: dict[str, str], user: AquareaEndUserJSON) -> dict[str, str]:
        """
        Generate HA discovery for all Aquarea settings.

        Routing:
          - single "Request" option          → button
          - ≤2 options with On/Off/Request   → switch
          - everything else with options     → select
          - no options + numeric value       → number
        """
        config: dict[str, str] = {}

        # Collect settings that have /options (button/switch/select)
        settings_with_options: set[str] = set()
        for k in topics:
            if "/settings/" in k and k.endswith("/options"):
                parts = k.split("/")
                if len(parts) >= 4:
                    settings_with_options.add(parts[3])

        for k, v in topics.items():
            if "/settings/" not in k:
                continue
            if k.endswith("/options") or k.endswith("/label"):
                continue

            parts = k.split("/")
            if len(parts) < 4:
                continue

            device_id = parts[1]
            name = parts[3]
            state_topic = k
            label = topics.get(state_topic + "/label", name)

            # Use the internal ASCII name for the MQTT topic slug, and the
            # translated label only for the HA friendly display name.
            # This avoids non-ASCII characters (French accents, etc.) in
            # discovery topics regardless of the Panasonic account language.
            topic_name = name   # always ASCII (e.g. "RoomHeater")
            display_name = label  # human-friendly, may contain accents

            # No /options → number if value is numeric
            if name not in settings_with_options:
                try:
                    float(v)
                except (ValueError, TypeError):
                    continue
                tr_entry = next(
                    (e for e in self.translation.values() if e.name == name), None
                )
                unit = tr_entry.unit if tr_entry else ""
                min_val = tr_entry.min if tr_entry else -128
                max_val = tr_entry.max if tr_entry else 127
                step = tr_entry.step if tr_entry else 1
                ha_topic, ha_data = encode_number(topic_name, device_id, state_topic, unit=unit, display_name=display_name, min_val=min_val, max_val=max_val, step=step)
                config[ha_topic] = ha_data
                continue

            # Has /options -> button / switch / select (process only the /options topic)
            options_v = topics.get(state_topic + "/options", "")
            values = [opt.strip() for opt in options_v.split("\n") if opt.strip()]
            if not values:
                continue

            if len(values) == 1:
                ha_topic, ha_data = encode_button(topic_name, device_id, state_topic, values[0], display_name=display_name)
                config[ha_topic] = ha_data
                continue

            has_on_off = any("Off" in val or "On" in val for val in values)

            if len(values) <= 2 and has_on_off:
                try:
                    ha_topic, ha_data = encode_switch(topic_name, device_id, state_topic, values, display_name=display_name)
                    config[ha_topic] = ha_data
                except ValueError:
                    ha_topic, ha_data = encode_select(topic_name, device_id, state_topic, values, display_name=display_name)
                    config[ha_topic] = ha_data
            else:
                ha_topic, ha_data = encode_select(topic_name, device_id, state_topic, values, display_name=display_name)
                config[ha_topic] = ha_data

        return config

    def encode_sensors(self, topics: dict[str, str], user: AquareaEndUserJSON) -> dict[str, str]:
        config: dict[str, str] = {}
        no_dupes: dict[str, str] = {}

        # De-duplicate: prefer /unit topic when present
        for k, v in topics.items():
            if "/log/" not in k and "/state/" not in k:
                continue
            if k.endswith("/unit"):
                no_dupes[k] = v
            elif f"{k}/unit" not in topics:
                no_dupes[k] = v

        for k, v in no_dupes.items():
            # Skip per-sensor availability topics — they are control topics,
            # not data topics, and should never become HA sensor entities.
            if k.endswith("/availability"):
                continue
            parts = k.split("/")
            if len(parts) < 4:
                continue
            name = parts[3]
            device_id = parts[1]

            is_live = "/state/" in k
            suffix = "Live" if is_live else "Log"
            display_name = f"{name} {suffix}"

            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
            object_id = f"{clean_name}_{suffix.lower()}"

            try:
                if k.endswith("/unit"):
                    unit = v
                    _, ha_data = encode_sensor(display_name, device_id, k.removesuffix("/unit"), unit)
                    component = "sensor"
                elif name == "Timestamp":
                    _, ha_data = encode_sensor(display_name, device_id, k, "", "timestamp")
                    component = "sensor"
                elif v in ("On", "Off"):
                    _, ha_data = encode_binary_sensor(display_name, device_id, k)
                    component = "binary_sensor"
                else:
                    unit = UNIT_MAP.get(name, "")
                    _, ha_data = encode_sensor(display_name, device_id, k, unit)
                    component = "sensor"

                data_dict = json.loads(ha_data)
                data_dict["unique_id"] = f"{device_id}_{object_id}"
                data_dict["name"] = display_name

                ha_topic = f"homeassistant/{component}/{device_id}/{object_id}/config".replace(" ", "")
                config[ha_topic] = json.dumps(data_dict)

            except Exception as exc:
                logger.debug(
                    "Failed to encode discovery for sensor '%s' (device %s, topic %s): %s",
                    name, device_id, k, exc,
                )

        return config