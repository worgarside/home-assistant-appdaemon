"""Raw paho MQTT transport and current Home Assistant discovery definitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from .models import MAX_TEMPLATE_LENGTH, MOOD_OPTIONS, CompletionMode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from paho.mqtt.properties import Properties
    from paho.mqtt.reasoncodes import ReasonCode

    from .models import HabitConfig


@dataclass(frozen=True, slots=True)
class MqttSettings:
    """MQTT connection and topic settings."""

    host: str
    port: int
    username: str | None
    password: str | None
    discovery_prefix: str
    base_topic: str
    qos: int = 0


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """One MQTT discovery entity."""

    component: str
    key: str
    name: str
    extra: dict[str, object]
    configurable: bool = True


class HabitMqtt:
    """Publish retained discovery/state and safely forward commands."""

    def __init__(
        self,
        settings: MqttSettings,
        *,
        on_command: Callable[[str, str], None],
        dispatch: Callable[[Callable[..., None], str, str], None],
        on_connect: Callable[[], None],
        log: Callable[..., None],
    ) -> None:
        self.settings = settings
        self._on_command = on_command
        self._dispatch = dispatch
        self._connected_callback = on_connect
        self._log = log
        self.client = paho.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="appdaemon-habit-tracker",
        )
        if settings.username:
            self.client.username_pw_set(settings.username, settings.password)
        self.client.on_connect = self._handle_connect
        self.client.on_message = self._handle_message
        self.client.will_set(
            self.topic("availability"),
            "offline",
            qos=settings.qos,
            retain=True,
        )

    def connect(self) -> None:
        """Connect and start paho's network thread."""
        self.client.connect(self.settings.host, self.settings.port, keepalive=60)
        self.client.loop_start()

    def disconnect(self) -> None:
        """Publish a clean shutdown and stop paho."""
        self.publish(self.topic("availability"), "offline")
        self.client.loop_stop()
        self.client.disconnect()

    def topic(self, suffix: str) -> str:
        """Build a topic below the configured base."""
        return f"{self.settings.base_topic}/{suffix}"

    def publish(self, topic: str, payload: object, *, retain: bool = True) -> None:
        """Publish JSON or scalar data."""
        serialized = payload if isinstance(payload, str) else json.dumps(payload)
        self.client.publish(
            topic,
            serialized,
            qos=self.settings.qos,
            retain=retain,
        )

    def publish_slot(self, user: str, config: HabitConfig) -> None:
        """Publish named discovery and configuration state for one slot."""
        display = config.name.strip() or "New habit"
        prefix = f"{user}_habit_{config.slot}"
        state_prefix = self.topic(f"{user}/{config.slot}")
        is_binary = str(config.habit_type) == "binary"
        state_spec = EntitySpec(
            "switch" if is_binary else "number",
            "state" if is_binary else "count",
            display,
            ({} if is_binary else {"min": 0, "max": 100000, "step": 1, "mode": "box"}),
            configurable=False,
        )
        specs = (
            EntitySpec("text", "name", f"{display} name", {}),
            EntitySpec(
                "select",
                "type",
                f"{display} type",
                {"options": ["binary", "countable"]},
            ),
            state_spec,
            EntitySpec("time", "reminder_time", f"{display} reminder time", {}),
            EntitySpec(
                "datetime",
                "next_reminder",
                f"{display} next reminder",
                {"icon": "mdi:bell-ring-outline"},
                configurable=False,
            ),
            EntitySpec(
                "number",
                "repeat_count",
                f"{display} repeat count",
                {"min": 0, "max": 100, "step": 1, "mode": "box"},
            ),
            EntitySpec(
                "number",
                "repeat_interval",
                f"{display} repeat interval",
                {
                    "min": 1,
                    "max": 1440,
                    "step": 1,
                    "mode": "box",
                    "unit_of_measurement": "min",
                },
            ),
            EntitySpec(
                "number",
                "streak_min_days",
                f"{display} minimum days per week",
                {"min": 1, "max": 7, "step": 1, "mode": "box"},
            ),
            EntitySpec("switch", "ai", f"{display} AI reminders", {}),
            EntitySpec(
                "switch",
                "end_of_day_reminder",
                f"{display} end-of-day reminder",
                {"icon": "mdi:weather-sunset-down"},
            ),
            EntitySpec(
                "select",
                "completion_mode",
                f"{display} completion mode",
                {"options": [str(mode) for mode in CompletionMode]},
            ),
            EntitySpec(
                "text",
                "completion_template",
                f"{display} completion template",
                {"max": MAX_TEMPLATE_LENGTH, "icon": "mdi:code-braces"},
            ),
            EntitySpec(
                "number",
                "completion_duration",
                f"{display} completion duration",
                {
                    "min": 1,
                    "max": 1440,
                    "step": 1,
                    "mode": "box",
                    "unit_of_measurement": "min",
                },
            ),
            EntitySpec("text", "icon_on", f"{display} completed icon", {}),
            EntitySpec("text", "icon_active", f"{display} active icon", {}),
            EntitySpec("text", "icon_off", f"{display} incomplete icon", {}),
            EntitySpec("text", "icon_zero", f"{display} zero icon", {}),
            EntitySpec(
                "sensor",
                "streak",
                f"{display} streak",
                {"unit_of_measurement": "days", "icon": "mdi:fire"},
                configurable=False,
            ),
        )
        self._retire_opposite_state(user, config.slot, is_binary=is_binary)
        for spec in specs:
            object_id = f"{prefix}_{spec.key}"
            payload: dict[str, object] = {
                "name": spec.name,
                "unique_id": f"appdaemon_{object_id}",
                "default_entity_id": f"{spec.component}.{object_id}",
                "availability_topic": self.topic("availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": self._device(user),
                "origin": self._origin(),
                "state_topic": f"{state_prefix}/{spec.key}/state",
                **spec.extra,
            }
            if spec.component != "sensor":
                payload["command_topic"] = f"{state_prefix}/{spec.key}/set"
            if spec.component == "switch":
                payload.update({"payload_on": "ON", "payload_off": "OFF"})
            if spec.configurable:
                payload["entity_category"] = "config"
            if spec.key in {"name", "state", "count", "streak"}:
                payload["json_attributes_topic"] = f"{state_prefix}/{spec.key}/attributes"
            self.publish(self._config_topic(spec.component, object_id), payload)
        self.publish_config_state(user, config)

    def publish_config_state(self, user: str, config: HabitConfig) -> None:
        """Publish all editable values for one slot."""
        prefix = self.topic(f"{user}/{config.slot}")
        values: dict[str, object] = {
            "name": config.name,
            "type": config.habit_type,
            "reminder_time": config.reminder_time,
            "repeat_count": config.repeat_count,
            "repeat_interval": config.repeat_interval_minutes,
            "streak_min_days": config.streak_min_days_per_week,
            "ai": "ON" if config.ai_enabled else "OFF",
            "end_of_day_reminder": (
                "ON" if config.end_of_day_reminder_enabled else "OFF"
            ),
            "icon_on": config.icon_on,
            "icon_active": config.icon_active,
            "icon_off": config.icon_off,
            "icon_zero": config.icon_zero,
            "completion_mode": config.completion_mode,
            "completion_template": config.completion_template,
            "completion_duration": config.completion_duration_minutes,
        }
        for key, value in values.items():
            self.publish(f"{prefix}/{key}/state", str(value))

    def retire_slot(self, user: str, slot: int) -> None:
        """Retire every retained discovery config for an unconfigured slot."""
        for component, key in _slot_entity_keys():
            self.publish(
                self._config_topic(component, f"{user}_habit_{slot}_{key}"),
                "",
            )

    def publish_mood_discovery(self, users: Iterable[str]) -> None:
        """Publish user-level mood and habit-count entities."""
        for user in users:
            display = user.title()
            for spec in (
                EntitySpec(
                    "select",
                    "mood_today",
                    "Mood today",
                    {"options": list(MOOD_OPTIONS)},
                ),
                EntitySpec("text", "mood_note", "Mood note", {}),
                EntitySpec(
                    "sensor",
                    "mood_streak",
                    "Mood streak",
                    {"unit_of_measurement": "days", "icon": "mdi:fire"},
                    configurable=False,
                ),
                EntitySpec(
                    "time",
                    "mood_reminder_time",
                    "Mood reminder time",
                    {},
                ),
                EntitySpec(
                    "switch",
                    "mood_reminders",
                    "Mood reminders",
                    {},
                ),
                EntitySpec(
                    "datetime",
                    "mood_next_reminder",
                    "Mood next reminder",
                    {"icon": "mdi:bell-ring-outline"},
                    configurable=False,
                ),
                EntitySpec(
                    "number",
                    "mood_repeat_count",
                    "Mood repeat count",
                    {"min": 0, "max": 100, "step": 1, "mode": "box"},
                ),
                EntitySpec(
                    "number",
                    "mood_repeat_interval",
                    "Mood repeat interval",
                    {
                        "min": 1,
                        "max": 1440,
                        "step": 1,
                        "mode": "box",
                        "unit_of_measurement": "min",
                    },
                ),
                EntitySpec(
                    "sensor",
                    "habits_binary_count",
                    f"{display} | Habits Binary Count",
                    {"icon": "mdi:toggle-switch"},
                    configurable=False,
                ),
                EntitySpec(
                    "sensor",
                    "habits_countable_count",
                    f"{display} | Habits Countable Count",
                    {"icon": "mdi:numeric"},
                    configurable=False,
                ),
            ):
                object_id = f"{user}_{spec.key}"
                state_path = (
                    f"{user}/{spec.key}"
                    if spec.key.startswith("habits_")
                    else f"{user}/mood/{spec.key}"
                )
                payload: dict[str, object] = {
                    "name": spec.name,
                    "unique_id": f"appdaemon_{object_id}",
                    "default_entity_id": f"{spec.component}.{object_id}",
                    "state_topic": self.topic(f"{state_path}/state"),
                    "availability_topic": self.topic("availability"),
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": self._device(user),
                    "origin": self._origin(),
                    **spec.extra,
                }
                if spec.component != "sensor":
                    payload["command_topic"] = self.topic(f"{state_path}/set")
                if spec.component == "switch":
                    payload.update({"payload_on": "ON", "payload_off": "OFF"})
                if spec.configurable:
                    payload["entity_category"] = "config"
                self.publish(self._config_topic(spec.component, object_id), payload)

    def subscribe_commands(self) -> None:
        """Subscribe to slot and mood commands."""
        self.client.subscribe(self.topic("+/+/+/set"), qos=self.settings.qos)

    def _handle_connect(
        self,
        _client: paho.Client,
        _userdata: object,
        _flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            self._log("MQTT connection failed: %s", reason_code)
            return
        self.subscribe_commands()
        self.publish(self.topic("availability"), "online")
        self._dispatch(self._connected_adapter, "", "")

    def _connected_adapter(self, _topic: str, _payload: str) -> None:
        self._connected_callback()

    def _handle_message(
        self,
        _client: paho.Client,
        _userdata: object,
        message: paho.MQTTMessage,
    ) -> None:
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            self._log("Ignoring non-UTF-8 MQTT command on %s", message.topic)
            return
        self._dispatch(self._on_command, str(message.topic), payload)

    def _retire_opposite_state(
        self,
        user: str,
        slot: int,
        *,
        is_binary: bool,
    ) -> None:
        component, key = ("number", "count") if is_binary else ("switch", "state")
        self.publish(
            self._config_topic(component, f"{user}_habit_{slot}_{key}"),
            "",
        )

    def _config_topic(self, component: str, object_id: str) -> str:
        return f"{self.settings.discovery_prefix}/{component}/{object_id}/config"

    @staticmethod
    def _device(user: str) -> dict[str, object]:
        return {
            "identifiers": [f"appdaemon_habits_{user}"],
            "manufacturer": "AppDaemon",
            "model": "Habit Tracker",
            "name": f"{user.title()} Habits",
        }

    @staticmethod
    def _origin() -> dict[str, str]:
        return {
            "name": "AppDaemon Habit Tracker",
            "sw": "home-assistant-appdaemon",
        }


def _slot_entity_keys() -> tuple[tuple[str, str], ...]:
    return (
        ("text", "name"),
        ("select", "type"),
        ("switch", "state"),
        ("number", "count"),
        ("time", "reminder_time"),
        ("datetime", "next_reminder"),
        ("number", "repeat_count"),
        ("number", "repeat_interval"),
        ("number", "streak_min_days"),
        ("switch", "ai"),
        ("switch", "end_of_day_reminder"),
        ("text", "icon_on"),
        ("text", "icon_active"),
        ("text", "icon_off"),
        ("text", "icon_zero"),
        ("select", "completion_mode"),
        ("text", "completion_template"),
        ("number", "completion_duration"),
        ("sensor", "streak"),
    )
