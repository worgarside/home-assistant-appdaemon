# Home Assistant: AppDaemon

## qBittorrent Storage Cleanup

`apps/qbittorrent/storage_cleanup.py` watches the qBittorrent scratch-storage
sensor. When usage crosses 99.9%, it asks qBittorrent for the completed torrents
currently seeding and ranks them by the nearer of their effective ratio and seeding
time limits. The notification identifies the nearest candidate by name, size, and
ratio and includes an action to delete that exact torrent and its content.
Torrents currently transferring upload data are skipped, and this is checked again
when the notification action is pressed.

After a confirmed deletion, the app waits 90 seconds for the storage sensor to
refresh. It then re-arms the threshold and offers the next eligible torrent only if
usage is still at or above 99.9%, with a separate confirmation required each time.
Once usage falls below the threshold, the app finds torrents in qBittorrent's
`errored` filter and starts them again. This recovers downloads that stopped when
the scratch disk ran out of space; qBittorrent 4's `resume` and qBittorrent 5's
`start` Web API operations are both supported.

Ratio progress has a configurable `1.25` weighting when candidates are ordered. A
torrent at ratio `4 / 5` therefore ranks alongside one at its full seeding-time limit,
while notification progress remains the unweighted value of the actual limit.

The app re-fetches the seeding list before acting on the notification, so it never
substitutes a newly ranked torrent for the one that was confirmed. Both qBittorrent
v1 and v2 info hashes are accepted. The global limits are read from qBittorrent at
runtime; per-torrent overrides and disabled limits are respected.

Add these values to `/homeassistant/secrets.yaml`, which is shared with AppDaemon:

```yaml
qbittorrent_url: http://<qBittorrent-host>:<Web-UI-port>
qbittorrent_username: <Web-UI-username>
qbittorrent_password: <Web-UI-password>
```

The configured qBittorrent user must be allowed to list, start, and delete torrents
and their content. No additional Python runtime dependency is required.

## Student Loan (SLC)

`apps/slc/slc_balance.py` polls the UK Student Loans Company account overview and
publishes eight Home Assistant MQTT sensors under one `Student Loan` device.

### AppDaemon Runtime Dependencies

Install `httpx2` and `paho-mqtt` into the same Python environment that runs
AppDaemon. For the Home Assistant AppDaemon runtime, add them to the runtime package
list, for example:

```yaml
python_packages:
  - httpx2
  - paho-mqtt
```

### Secrets

```yaml
slc_username: <email or customer reference number>
slc_password: <password>
slc_secret_answer: <secret answer>
appdaemon_mqtt_host: <mqtt broker host>
appdaemon_mqtt_username: <mqtt username>
appdaemon_mqtt_password: <mqtt password>
```

### Entities

- `sensor.slc_balance`
- `sensor.slc_interest_rate`
- `sensor.slc_as_of_date`
- `sensor.slc_current_year`
- `sensor.slc_salary_repayments`
- `sensor.slc_direct_repayments`
- `sensor.slc_interest_added`
- `sensor.slc_last_successful_scrape`

Optional diagnostic entity: `sensor.slc_last_poll`.

Default poll interval is 6 hours. MQTT discovery must be enabled in Home Assistant.

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

### Diagnostics

The AppDaemon app exposes the AC through MQTT climate discovery and the optional
diagnostic raw sensor. Use the raw sensor attributes and AppDaemon logs to inspect
the latest TinyTuya payload when troubleshooting.

### Availability and State

MQTT state topics are retained so Home Assistant can restore the climate entity quickly
after restart.

If the AC is unreachable, the app logs the TinyTuya error, marks availability offline
on MQTT, updates the raw sensor with error details when configured, and retries on the
next scheduled poll.
