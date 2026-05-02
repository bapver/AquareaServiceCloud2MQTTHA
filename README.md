# AquareaServiceCloud2MQTTHA

Home Assistant add-on that bridges your **Panasonic Aquarea** heat pump to Home Assistant via MQTT, using the [Aquarea Service Cloud](https://aquarea-service.panasonic.com/) API.

No local gateway required — the add-on communicates directly with Panasonic's cloud, exactly as the Aquarea Service Cloud website does.

---

## Features

- **Live status** — real-time sensors (temperatures, pump speed, flow rate, mode, compressor…)
- **Log sensors** — sensors from the Aquarea diagnostic log (temperatures, pressures, energy, timers…)
- **Full settings control** — switches, selects, buttons and numeric inputs for all user-accessible settings
- **Numeric settings with correct ranges** — tank target temperature, zone temperatures and holiday shifts have accurate min/max bounds, fetched dynamically from Panasonic's own JS at startup
- **Home Assistant MQTT discovery** — all entities appear automatically with correct types, units and device grouping
- **Multi-language** — sensor and setting labels follow the Panasonic API language (configurable)
- **Multi-device** — accounts with several heat pumps are fully supported; each device gets its own topics and HA device
- **100% dynamic** — sensor names, units, binary values and numeric ranges all come from the Panasonic API; no hardcoded sensor list
- **SSL verified** — communicates securely with Panasonic servers by default

---

## Prerequisites

- A [Mosquitto MQTT broker](https://github.com/home-assistant/addons/tree/master/mosquitto) add-on (or any MQTT broker)
- An [Aquarea Service Cloud](https://aquarea-service.panasonic.com/) account with your heat pump registered
- MQTT integration enabled in Home Assistant

---

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click **⋮ → Repositories**
3. Add: `https://github.com/baptisteverger/AquareaServiceCloud2MQTTHA`
4. Find **AquareaServiceCloud2MQTTHA** and click **Install**
5. Configure (see below) and click **Start**

---

## Configuration

Edit the add-on configuration in **Settings → Add-ons → AquareaServiceCloud2MQTTHA → Configuration**.

| Parameter | Description | Default |
|---|---|---|
| `AquareaServiceCloudURL` | Panasonic API base URL | `https://aquarea-service.panasonic.com/` |
| `AquareaServiceCloudLogin` | Your Aquarea Service Cloud email | *(required)* |
| `AquareaServiceCloudPassword` | Your Aquarea Service Cloud password | *(required)* |
| `AquareaServiceCloudSSLVerify` | Verify SSL certificate (disable only for debugging) | `true` |
| `AquareaTimeout` | HTTP request timeout in seconds | `30` |
| `PoolInterval` | Polling interval in seconds | `60` |
| `LogSecOffset` | How many seconds back to fetch log data | `3600` |
| `MqttServer` | MQTT broker hostname or IP | *(required)* |
| `MqttPort` | MQTT broker port | `1883` |
| `MqttLogin` | MQTT username (leave empty if none) | `` |
| `MqttPass` | MQTT password (leave empty if none) | `` |
| `MqttClientID` | MQTT client identifier | `aquarea-ServiceCloud` |
| `MqttKeepalive` | MQTT keepalive in seconds | `60` |
| `Language` | Label language for sensor/setting names | `en` |
| `LogLevel` | Logging verbosity: `DEBUG`, `INFO`, `WARNING` | `INFO` |

### Language codes

`en` · `fr` · `de` · `es` · `it` · `nl` · `pl` · `pt` · `cs` · `sv` · `fi` · `nb` · `da` · `el` · `ro` · `sk` · `sl` · `hr` · `bg` · `hu` · `tr`

> **Note:** changing the language after initial setup will rename all entities in HA. Delete the existing Aquarea device from the MQTT integration before restarting the add-on.

---

## MQTT Topics

All topics are prefixed with `aquarea/{device_id}/` where `device_id` is the Panasonic device serial number.

### Live status
```
aquarea/{device_id}/state/{SensorName}              → current value (string)
```

### Log data (most recent diagnostic entry)
```
aquarea/{device_id}/log/{SensorName}                → value
aquarea/{device_id}/log/{SensorName}/unit           → unit (°C, kW, Hz, bar…)
aquarea/{device_id}/log/Timestamp                   → ISO 8601 UTC timestamp
aquarea/{device_id}/log/CurrentError                → error code (0 = no error)
```

### Settings (read/write)
```
aquarea/{device_id}/settings/{Name}                 → current value
aquarea/{device_id}/settings/{Name}/options         → available options (newline-separated)
aquarea/{device_id}/settings/{Name}/label           → display label
aquarea/{device_id}/settings/{Name}/set             → command topic (write here to change)
```

### Availability
```
aquarea/status    → "online" / "offline"
```

---

## Entities created in Home Assistant

Controls
Sensors

---

## How it works

```
Panasonic Aquarea Service Cloud
        ↕  HTTPS (same API as the website)
AquareaServiceCloud2MQTTHA (this add-on)
        ↕  MQTT
Home Assistant MQTT integration
        ↕  MQTT discovery
HA entities (sensors, switches, selects, numbers, buttons)
```

At startup the add-on:
1. Logs in to Aquarea Service Cloud and fetches the live type-2010 dictionary (sensor labels)
2. Fetches the Panasonic settings JavaScript bundle to extract numeric setting ranges for your model
3. For each device on the account: fetches status, log schema, settings and publishes all data + HA discovery messages
4. Polls every `PoolInterval` seconds

---

## Troubleshooting

**No entities appear in HA**
- Verify the MQTT broker is reachable and credentials are correct
- Ensure MQTT discovery is enabled in the HA MQTT integration (enabled by default)
- Check the add-on log for errors

**Sensors show `Unknown` or `-78 °C`**
- This is normal for sensors that correspond to optional hardware not installed on your unit (Zone 2, buffer tank, solar panel, pool, bivalent system)
- Values like `-78`, `-31`, `-46` are Panasonic sentinel values for unconnected physical sensors; they are published as-is

**Number entities still show 0–100 range after update**
- HA caches entity properties — delete the old number entities from the MQTT integration and restart the add-on

**Changing `Language` breaks entity names**
- Delete the existing Aquarea device from **Settings → Devices & Services → MQTT**, then restart the add-on

---

## Security

- Credentials are stored in `options.json` on your Home Assistant host only — never committed to Git (the file is in `.gitignore`)
- The add-on verifies Panasonic's SSL certificate by default (`AquareaServiceCloudSSLVerify: true`)

---

## Credits

- Original Go implementation: [kamaradclimber/Aquarea2mqtt](https://github.com/kamaradclimber/Aquarea2mqtt)
