# Home Assistant: AppDaemon

## Pro Breeze Portable AC

`apps/pro_breeze_ac/pro_breeze_ac.py` controls a Pro Breeze portable air conditioner locally with
TinyTuya. It is intended for Tuya local protocol `3.5` devices that are not working
reliably through `localtuya`.

### AppDaemon Runtime Dependencies

Install `tinytuya` and `paho-mqtt` into the same Python environment that runs
AppDaemon. For the Home Assistant AppDaemon runtime, add them to the runtime package
list, for example:

```yaml
python_packages:
  - tinytuya
  - paho-mqtt
```

If AppDaemon runs in a venv or container, install it there instead:

```shell
pip install tinytuya paho-mqtt
```

### MQTT Climate Entity

The app publishes Home Assistant MQTT discovery for `climate.pro_breeze_ac` and uses
MQTT command topics to translate native climate service calls back to Tuya DPS writes.
MQTT discovery must be enabled in Home Assistant.

Configure an MQTT user for AppDaemon and provide these secrets:

```yaml
appdaemon_mqtt_host: <mqtt broker host>
appdaemon_mqtt_username: <mqtt username>
appdaemon_mqtt_password: <mqtt password>
```

The optional `raw_sensor` is still useful for diagnostics. It is updated with a compact
state and stores the latest TinyTuya payload in attributes: `raw_status`, `dps`,
`known_dps`, and `last_updated`.

### Confirmed DP Map

- `dp_power`: `1`, boolean power.
- `dp_target_temp`: `2`, target temperature in Celsius, writable range `16`-`32`.
- `dp_current_temp`: `3`, ambient temperature in Celsius, read-only.
- `dp_mode`: `4`, values `Cool`, `Dry`, and `Fan`.
- `dp_fan`: `5`, values `High`, `Mid`, and `Low`.
- `dp_swing`: `15`, values `ON` and `OFF`.
- `dp_sleep`: `101`, boolean sleep mode, exposed as climate preset `sleep`.

DP IDs are treated as strings internally because TinyTuya status payloads commonly
use string keys under `dps`.

### Discovery Workflow

For careful manual DP testing, use `_local_sandbox/tuya_test.py`. The AppDaemon app
itself only exposes the AC through MQTT climate discovery and the optional diagnostic
raw sensor.

### Availability and State

MQTT state topics are retained so Home Assistant can restore the climate entity quickly
after restart.

If the AC is unreachable, the app logs the TinyTuya error, marks availability offline
on MQTT, updates the raw sensor with error details when configured, and retries on the
next scheduled poll.
