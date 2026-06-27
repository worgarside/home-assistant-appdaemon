"""Control a Pro Breeze portable AC via TinyTuya."""

from __future__ import annotations

from datetime import UTC, datetime
from json import dumps
from typing import Any, Final

import appdaemon.plugins.hass.hassapi as hass
import paho.mqtt.client as mqtt
import tinytuya  # pyright: ignore[reportMissingTypeStubs]
from paho.mqtt.enums import CallbackAPIVersion

COMMAND_REFRESH_DELAY: Final[float] = 2.0
MIN_TARGET_TEMP: Final[int] = 16
MAX_TARGET_TEMP: Final[int] = 32
TUYA_MODE_TO_HVAC_MODE: Final[dict[str, str]] = {
    "Cool": "cool",
    "Dry": "dry",
    "Fan": "fan_only",
}
HVAC_MODE_TO_TUYA_MODE: Final[dict[str, str]] = {
    value: key for key, value in TUYA_MODE_TO_HVAC_MODE.items()
}
TUYA_FAN_TO_HA_FAN: Final[dict[str, str]] = {
    "High": "high",
    "Mid": "medium",
    "Low": "low",
}
HA_FAN_TO_TUYA_FAN: Final[dict[str, str]] = {
    value: key for key, value in TUYA_FAN_TO_HA_FAN.items()
}
PRESET_NONE: Final[str] = "none"
PRESET_SLEEP: Final[str] = "sleep"


class ProBreezeAC(hass.Hass):
    """Control and monitor a Pro Breeze portable AC using TinyTuya."""

    device: Any | None
    dp_current_temp: str | None
    dp_fan: str | None
    dp_mode: str | None
    dp_power: str | None
    dp_sleep: str | None
    dp_swing: str | None
    dp_target_temp: str | None
    mqtt_client: Any | None
    raw_sensor: str | None
    version: float

    def initialize(self) -> None:
        """Initialize the app."""
        self.device = None
        self.mqtt_client = None
        self._last_raw_status_json: str | None = None

        self.device_id = self.args["device_id"]
        self.local_key = self.args["local_key"]
        self.ip = self.args["ip"]
        self.version = float(self.args.get("version", 3.5))
        self.poll_interval = int(self.args.get("poll_interval", 30))

        self.raw_sensor = self.args.get("raw_sensor")

        self.dp_power = self._get_dp("dp_power")
        self.dp_target_temp = self._get_dp("dp_target_temp")
        self.dp_current_temp = self._get_dp("dp_current_temp")
        self.dp_mode = self._get_dp("dp_mode")
        self.dp_fan = self._get_dp("dp_fan")
        self.dp_swing = self._get_dp("dp_swing")
        self.dp_sleep = self._get_dp("dp_sleep")

        self._configure_mqtt()

        self.run_every(self.poll_device, "now", self.poll_interval)

        self.log(
            "Initialized Pro Breeze AC polling every %s seconds; configured DPS: %s",
            self.poll_interval,
            self._configured_dps_summary(),
        )

    def terminate(self) -> None:
        """Cleanly disconnect the MQTT client on AppDaemon shutdown."""
        if self.mqtt_client is None:
            return

        self._publish_mqtt_availability(is_available=False)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    def poll_device(self, kwargs: dict[str, Any] | None = None) -> None:
        """Poll the AC and sync mapped DPS values into Home Assistant."""
        del kwargs

        status = self._read_status()
        if status is None:
            return

        dps = self._extract_dps(status)
        self._write_raw_sensor("updated", status, dps=dps)
        self._set_availability(is_available=True)
        self._log_status_if_changed(status, dps)

        self._publish_mqtt_climate_state(dps)

    def handle_mqtt_command(self, kwargs: dict[str, Any]) -> None:
        """Handle a command received from the MQTT climate entity."""
        topic = str(kwargs["topic"])
        payload = str(kwargs["payload"]).strip()
        command = self._mqtt_command_for_topic(topic)

        self.log("MQTT climate command received: %s => %r", command, payload)

        if command == "mode":
            self._handle_mqtt_mode_command(payload)
            return

        if command == "temperature":
            self._handle_mqtt_temperature_command(payload)
            return

        if command == "fan_mode":
            self._handle_mqtt_fan_mode_command(payload)
            return

        if command == "swing_mode":
            self._handle_mqtt_swing_mode_command(payload)
            return

        if command == "preset_mode":
            self._handle_mqtt_preset_mode_command(payload)
            return

        self.error("Unknown MQTT climate command topic: %s", topic)

    def _get_dp(self, key: str) -> str | None:
        value = self.args.get(key)
        return None if value is None else str(value)

    def _configure_mqtt(self) -> None:
        self.mqtt_host = self.args.get("mqtt_host")
        self.mqtt_port = int(self.args.get("mqtt_port", 1883))
        self.mqtt_username = self.args.get("mqtt_username")
        self.mqtt_password = self.args.get("mqtt_password")
        self.mqtt_discovery_prefix = str(
            self.args.get("mqtt_discovery_prefix", "homeassistant"),
        ).strip("/")
        self.mqtt_object_id = str(self.args.get("mqtt_object_id", "pro_breeze_ac"))
        self.mqtt_unique_id = str(self.args.get("mqtt_unique_id", self.mqtt_object_id))
        self.mqtt_name = str(self.args.get("mqtt_name", "Pro Breeze AC"))
        self.mqtt_base_topic = str(
            self.args.get("mqtt_base_topic", f"appdaemon/{self.mqtt_object_id}"),
        ).strip("/")
        self.mqtt_qos = int(self.args.get("mqtt_qos", 0))
        self._mqtt_command_topics = {
            "mode": f"{self.mqtt_base_topic}/mode/set",
            "temperature": f"{self.mqtt_base_topic}/temperature/set",
            "fan_mode": f"{self.mqtt_base_topic}/fan_mode/set",
            "swing_mode": f"{self.mqtt_base_topic}/swing_mode/set",
            "preset_mode": f"{self.mqtt_base_topic}/preset_mode/set",
        }

        if not self.mqtt_host:
            self.log("No mqtt_host configured; MQTT climate discovery disabled")
            return

        client_id = str(
            self.args.get("mqtt_client_id", f"appdaemon-{self.mqtt_object_id}"),
        )
        self.mqtt_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        if self.mqtt_username:
            self.mqtt_client.username_pw_set(self.mqtt_username, self.mqtt_password)

        self.mqtt_client.on_connect = self._handle_mqtt_connect
        self.mqtt_client.on_message = self._handle_mqtt_message
        self.mqtt_client.will_set(
            self._mqtt_topic("availability"),
            "offline",
            qos=self.mqtt_qos,
            retain=True,
        )

        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        except Exception as err:
            self.error(
                "Failed to connect to MQTT broker %s:%s: %s",
                self.mqtt_host,
                self.mqtt_port,
                err,
            )
            self.mqtt_client = None
            return

        self.mqtt_client.loop_start()
        self.log(
            "Configured MQTT climate discovery for %s at %s",
            self.mqtt_object_id,
            self.mqtt_base_topic,
        )

    def _handle_mqtt_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        del userdata, flags, properties

        is_failure = getattr(reason_code, "is_failure", None)
        failed = bool(is_failure) if isinstance(is_failure, bool) else reason_code != 0
        if failed:
            self.error(
                "MQTT connection failed for %s: %s",
                self.mqtt_object_id,
                reason_code,
            )
            return

        self.log("MQTT connected for %s", self.mqtt_object_id)
        self._publish_mqtt_discovery()
        self._publish_mqtt_availability(is_available=True)
        for topic in self._mqtt_command_topics.values():
            client.subscribe(topic, qos=self.mqtt_qos)

    def _handle_mqtt_message(
        self,
        client: Any,
        userdata: Any,
        message: Any,
    ) -> None:
        del client, userdata

        payload = message.payload.decode("utf-8")
        # Keep all TinyTuya socket access on AppDaemon's app thread.
        self.run_in(
            self.handle_mqtt_command,
            0,
            topic=str(message.topic),
            payload=payload,
        )

    def _mqtt_command_for_topic(self, topic: str) -> str | None:
        for command, command_topic in self._mqtt_command_topics.items():
            if topic == command_topic:
                return command

        return None

    def _mqtt_topic(self, topic: str) -> str:
        return f"{self.mqtt_base_topic}/{topic}"

    def _publish_mqtt(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        if self.mqtt_client is None:
            return

        if not isinstance(payload, str):
            payload = dumps(payload, sort_keys=True)

        self.mqtt_client.publish(topic, payload, qos=self.mqtt_qos, retain=retain)

    def _publish_mqtt_discovery(self) -> None:
        config_topic = (
            f"{self.mqtt_discovery_prefix}/climate/{self.mqtt_object_id}/config"
        )
        config = {
            "name": self.mqtt_name,
            "unique_id": self.mqtt_unique_id,
            "object_id": self.mqtt_object_id,
            "availability_topic": self._mqtt_topic("availability"),
            "payload_available": "online",
            "payload_not_available": "offline",
            "mode_command_topic": self._mqtt_command_topics["mode"],
            "mode_state_topic": self._mqtt_topic("mode/state"),
            "modes": ["off", "cool", "dry", "fan_only"],
            "temperature_command_topic": self._mqtt_command_topics["temperature"],
            "temperature_state_topic": self._mqtt_topic("temperature/state"),
            "current_temperature_topic": self._mqtt_topic("current_temperature/state"),
            "temperature_unit": "C",
            "min_temp": MIN_TARGET_TEMP,
            "max_temp": MAX_TARGET_TEMP,
            "temp_step": 1,
            "precision": 1.0,
            "fan_mode_command_topic": self._mqtt_command_topics["fan_mode"],
            "fan_mode_state_topic": self._mqtt_topic("fan_mode/state"),
            "fan_modes": ["high", "medium", "low"],
            "swing_mode_command_topic": self._mqtt_command_topics["swing_mode"],
            "swing_mode_state_topic": self._mqtt_topic("swing_mode/state"),
            "swing_modes": ["on", "off"],
            "preset_mode_command_topic": self._mqtt_command_topics["preset_mode"],
            "preset_mode_state_topic": self._mqtt_topic("preset_mode/state"),
            "preset_modes": [PRESET_SLEEP],
            "device": {
                "identifiers": [self.mqtt_unique_id],
                "manufacturer": "Pro Breeze",
                "model": "PB-AC-14",
                "name": self.mqtt_name,
            },
            "origin": {
                "name": "AppDaemon Pro Breeze AC",
                "sw": "home-assistant-appdaemon",
            },
        }
        self._publish_mqtt(config_topic, config)

    def _publish_mqtt_availability(self, *, is_available: bool) -> None:
        self._publish_mqtt(
            self._mqtt_topic("availability"),
            "online" if is_available else "offline",
        )

    def _publish_mqtt_climate_state(self, dps: dict[str, Any]) -> None:
        if self.mqtt_client is None:
            return

        hvac_mode = self._hvac_mode_from_dps(dps)
        if hvac_mode is not None:
            self._publish_mqtt(self._mqtt_topic("mode/state"), hvac_mode)

        self._publish_numeric_state("temperature/state", self.dp_target_temp, dps)
        self._publish_numeric_state(
            "current_temperature/state",
            self.dp_current_temp,
            dps,
        )

        if self.dp_fan in dps:
            fan_mode = TUYA_FAN_TO_HA_FAN.get(str(dps[self.dp_fan]))
            if fan_mode:
                self._publish_mqtt(self._mqtt_topic("fan_mode/state"), fan_mode)

        if self.dp_swing in dps:
            self._publish_mqtt(
                self._mqtt_topic("swing_mode/state"),
                "on" if self._swing_from_dp(dps[self.dp_swing]) else "off",
            )

        if self.dp_sleep in dps:
            preset = (
                PRESET_SLEEP if self._state_to_bool(dps[self.dp_sleep]) else PRESET_NONE
            )
            self._publish_mqtt(self._mqtt_topic("preset_mode/state"), preset)

    def _publish_numeric_state(
        self,
        topic: str,
        dp: str | None,
        dps: dict[str, Any],
    ) -> None:
        if dp is None or dp not in dps:
            return

        value = self._float_or_none(dps[dp])
        if value is None:
            return

        self._publish_mqtt(self._mqtt_topic(topic), self._number_for_tuya(value))

    def _hvac_mode_from_dps(self, dps: dict[str, Any]) -> str | None:
        if self.dp_power in dps and not self._state_to_bool(dps[self.dp_power]):
            return "off"

        if self.dp_mode not in dps:
            return None

        return TUYA_MODE_TO_HVAC_MODE.get(str(dps[self.dp_mode]))

    def _handle_mqtt_mode_command(self, payload: str) -> None:
        if self.dp_power is None:
            self.log("No dp_power configured; skipping MQTT mode command")
            return

        if payload == "off":
            self._command_dp(self.dp_power, value=False, label="MQTT power")
            return

        tuya_mode = HVAC_MODE_TO_TUYA_MODE.get(payload)
        if tuya_mode is None:
            self.error("Unsupported MQTT HVAC mode command: %r", payload)
            return

        if self.dp_mode is None:
            self.log("No dp_mode configured; skipping MQTT mode command")
            return

        self._command_dp(self.dp_mode, tuya_mode, "MQTT mode")
        self._command_dp(self.dp_power, value=True, label="MQTT power")

    def _handle_mqtt_temperature_command(self, payload: str) -> None:
        if self.dp_target_temp is None:
            self.log("No dp_target_temp configured; skipping MQTT temperature command")
            return

        try:
            value = self._number_for_tuya(payload)
        except ValueError:
            self.error("Unsupported MQTT temperature command: %r", payload)
            return

        if not MIN_TARGET_TEMP <= float(value) <= MAX_TARGET_TEMP:
            self.error("Out-of-range MQTT temperature command: %r", payload)
            return

        self._command_dp(self.dp_target_temp, value, "MQTT target temperature")

    def _handle_mqtt_fan_mode_command(self, payload: str) -> None:
        if self.dp_fan is None:
            self.log("No dp_fan configured; skipping MQTT fan command")
            return

        value = HA_FAN_TO_TUYA_FAN.get(payload)
        if value is None:
            self.error("Unsupported MQTT fan mode command: %r", payload)
            return

        self._command_dp(self.dp_fan, value, "MQTT fan")

    def _handle_mqtt_swing_mode_command(self, payload: str) -> None:
        if self.dp_swing is None:
            self.log("No dp_swing configured; skipping MQTT swing command")
            return

        if payload not in {"on", "off"}:
            self.error("Unsupported MQTT swing mode command: %r", payload)
            return

        self._command_dp(
            self.dp_swing,
            self._swing_to_dp(is_on=payload == "on"),
            "MQTT swing",
        )

    def _handle_mqtt_preset_mode_command(self, payload: str) -> None:
        if self.dp_sleep is None:
            self.log("No dp_sleep configured; skipping MQTT preset command")
            return

        if payload == PRESET_SLEEP:
            self._command_dp(self.dp_sleep, value=True, label="MQTT sleep preset")
            return

        if payload in {PRESET_NONE, "None", ""}:
            self._command_dp(self.dp_sleep, value=False, label="MQTT preset clear")
            return

        self.error("Unsupported MQTT preset mode command: %r", payload)

    def _configured_dps_summary(self) -> str:
        configured = {
            "power": self.dp_power,
            "target_temp": self.dp_target_temp,
            "current_temp": self.dp_current_temp,
            "mode": self.dp_mode,
            "fan": self.dp_fan,
            "swing": self.dp_swing,
            "sleep": self.dp_sleep,
        }
        return (
            ", ".join(f"{name}={dp}" for name, dp in configured.items() if dp) or "none"
        )

    def _get_device(self) -> Any:
        if self.device is not None:
            return self.device

        self.device = tinytuya.Device(
            dev_id=self.device_id,
            address=self.ip,
            local_key=self.local_key,
            version=self.version,
        )

        set_persistent = getattr(self.device, "set_socketPersistent", None)
        if callable(set_persistent):
            try:
                set_persistent(True)
                self.log("Enabled TinyTuya persistent socket")
            except Exception as err:
                self.error("Failed to enable TinyTuya persistent socket: %s", err)

        return self.device

    def _read_status(self) -> dict[str, Any] | None:
        try:
            status = self._get_device().status()
        except Exception as err:
            self.error("TinyTuya status failed: %s", err)
            self._handle_tuya_failure(err)
            return None

        if not isinstance(status, dict):
            self.error("TinyTuya returned unexpected status payload: %r", status)
            self._write_raw_sensor("error", {"raw": status}, error="unexpected payload")
            self._set_availability(is_available=False)
            self._reset_device()
            return None

        if "dps" not in status:
            self.error("TinyTuya status did not include DPS: %s", status)
            self._log_status_if_changed(status, {})
            self._write_raw_sensor(
                "error",
                status,
                error=status.get("Error", "missing dps"),
            )
            self._set_availability(is_available=False)
            self._reset_device()
            return None

        return status

    def _handle_tuya_failure(self, err: Exception) -> None:
        self._write_raw_sensor("error", {}, error=str(err))
        self._set_availability(is_available=False)
        self._reset_device()

    def _reset_device(self) -> None:
        self.device = None

    def _extract_dps(self, status: dict[str, Any]) -> dict[str, Any]:
        dps = status.get("dps", {})
        if not isinstance(dps, dict):
            self.error("TinyTuya DPS payload was not a dictionary: %r", dps)
            return {}

        return {str(dp): value for dp, value in dps.items()}

    def _log_status_if_changed(
        self,
        status: dict[str, Any],
        dps: dict[str, Any],
    ) -> None:
        status_json = dumps(status, sort_keys=True, default=str)
        if status_json == self._last_raw_status_json:
            return

        self._last_raw_status_json = status_json
        self.log("Raw TinyTuya status: %s", status_json)
        self.log("Known DPS: %s", ", ".join(sorted(dps)) or "none")

    def _write_raw_sensor(
        self,
        state: str,
        status: dict[str, Any],
        *,
        dps: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if self.raw_sensor is None:
            return

        dps = dps or self._extract_dps(status)
        attributes = {
            "raw_status": status,
            "dps": dps,
            "known_dps": sorted(dps),
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if error:
            attributes["error"] = error

        self.set_state(self.raw_sensor, state=state, attributes=attributes)

    def _set_availability(self, *, is_available: bool) -> None:
        self._publish_mqtt_availability(is_available=is_available)

    def _command_dp(self, dp: str, value: Any, label: str) -> None:
        try:
            result = self._get_device().set_value(dp, value)
        except Exception as err:
            self.error("TinyTuya set_value failed for %s DP %s: %s", label, dp, err)
            self._handle_tuya_failure(err)
            return

        if isinstance(result, dict) and result.get("Error"):
            self.error("TinyTuya set_value error for %s DP %s: %s", label, dp, result)
            self._write_raw_sensor("error", result, error=str(result["Error"]))
            self._set_availability(is_available=False)
            self._reset_device()
            return

        self.log("Set %s DP %s => %r; result: %s", label, dp, value, result)
        self.run_in(self.poll_device, COMMAND_REFRESH_DELAY)

    @staticmethod
    def _swing_from_dp(value: Any) -> bool:
        if isinstance(value, str):
            return value.upper() == "ON"

        return ProBreezeAC._state_to_bool(value)

    @staticmethod
    def _swing_to_dp(*, is_on: bool) -> str:
        return "ON" if is_on else "OFF"

    @staticmethod
    def _state_to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.lower() in {"1", "on", "true", "yes"}

        return bool(value)

    @staticmethod
    def _number_for_tuya(value: Any) -> int | float:
        numeric_value = float(value)
        if numeric_value.is_integer():
            return int(numeric_value)

        return numeric_value

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if not isinstance(value, str | int | float) or isinstance(value, bool):
            return None

        try:
            return float(value)
        except ValueError:
            return None
