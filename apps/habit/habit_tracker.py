"""Disk-backed, MQTT-discovered habit and mood tracker."""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, cast

import appdaemon.plugins.hass.hassapi as hass

from .models import (
    MAX_DURATION_MINUTES,
    MAX_NAME_LENGTH,
    MAX_TEMPLATE_LENGTH,
    MOOD_OPTIONS,
    CompletionMode,
    HabitConfig,
    HabitType,
    PendingReminder,
    TemplateProgress,
    UnsupportedSchemaVersionError,
    UserData,
    calculate_mood_streak,
    calculate_streak,
    normalize_spare_slot,
)
from .mqtt import HabitMqtt, MqttSettings
from .reminders import (
    ReminderManager,
    end_of_day_reminder_fire_at,
    repeat_fits_before_midnight,
)
from .store import HabitStore
from .templates import TemplateWatcher, coerce_truthy, extract_candidate_entities

INVALID_STATES: Final[frozenset[object]] = frozenset(
    {None, "", "unknown", "unavailable"},
)
NEXT_REMINDER_ECHO_TOLERANCE_SECONDS: Final[float] = 2
AI_INSTRUCTIONS: Final[str] = """Write one short habit reminder notification message.

Tone: neutral, positive, and encouraging. Do not be sarcastic, preachy, or overly
casual. Do not invent facts.

Requirements:
- Return only the notification message text
- One or two short sentences maximum
- Mention the habit name naturally
- Do not address the recipient by name or with a personal greeting
- Treat the provided context as private guidance for choosing an appropriate reminder,
  not content to repeat to the recipient
- Do not state, summarize, or open with contextual facts such as the time, day, date,
  month, location, work status, calendar availability, activity, weather, or mood
- Use context only when it materially changes what a useful reminder would say
- Do not name specific places, addresses, or coordinates
- Do not include markdown, emojis, quotes, or a title"""
MOOD_AI_INSTRUCTIONS: Final[str] = """Write one short mood check-in reminder notification
message.

Tone: neutral, positive, and encouraging. Do not be sarcastic, preachy, or overly
casual. Do not invent facts.

Requirements:
- Return only the notification message text
- One or two short sentences maximum
- Ask the recipient to log how they are feeling today
- Do not address the recipient by name or with a personal greeting
- Treat the provided context as private guidance for choosing an appropriate reminder,
  not content to repeat to the recipient
- Do not state, summarize, or open with contextual facts such as the time, day, date,
  month, location, work status, calendar availability, activity, weather, or mood
- Use context only when it materially changes what a useful reminder would say
- Do not name specific places, addresses, or coordinates
- Do not include markdown, emojis, quotes, or a title"""
MOOD_FALLBACK_MESSAGE: Final[str] = "Don't forget to log how you're feeling today."

REQUIRED_USER_KEYS: Final[tuple[str, ...]] = (
    "notify_script",
    "dashboard_url",
    "person_entity",
    "at_work_entity",
    "workday_entity",
    "activity_entity",
    "weather_entity",
)
SLOT_TOPIC_PARTS: Final[int] = 4
ACTION_PARTS: Final[int] = 3
MAX_NETWORK_PORT: Final[int] = 65535
# Ticks on the minute, so a duration-based template reconciles once a minute
# even if one of its dependencies changes without emitting an event.
TIME_TICK_ENTITY: Final[str] = "sensor.time"
MAX_DEBOUNCE_SECONDS: Final[float] = 60


class HabitTracker(hass.Hass):
    """Manage dynamic habit entities, persistence, reminders, and mood."""

    mqtt: HabitMqtt | None

    def initialize(self) -> None:
        """Load disk state and start integrations."""
        self.users = tuple(str(user).lower() for user in self.args.get("users", ()))
        if not self.users:
            self.error("At least one user is required")
            return
        if not self._validate_config():
            return
        self.mqtt = None
        self.reminders_enabled = bool(self.args.get("reminders_enabled", False))
        self.template_evaluation_enabled = bool(
            self.args.get("template_evaluation_enabled", False),
        )
        self.template_eval_debounce_seconds = float(
            self.args.get("template_eval_debounce_seconds", 2),
        )
        try:
            self.store = HabitStore(
                Path(
                    str(
                        self.args.get(
                            "store_directory",
                            "/homeassistant/.appdaemon/habits",
                        ),
                    ),
                ),
                self.users,
                log=self.log,
                error=self.error,
            )
        except UnsupportedSchemaVersionError as error:
            # Stay inert: no timers, no MQTT, no save. The on-disk store is
            # intact and must not be overwritten by an empty one.
            self.error("Habit tracker disabled, store is incompatible: %s", error)
            return
        self._startup_retired: dict[str, tuple[int, ...]] = {}
        for user in self.users:
            _, retired = self._normalize_user_slots(user)
            self._startup_retired[user] = retired
        self.store.save()

        self.reminders = ReminderManager(self, self._reminder_callback)
        self.templates = TemplateWatcher(
            self,
            self._template_dependency_changed,
            self._template_evaluation_callback,
            self._duration_elapsed_callback,
        )
        self._watched_entities: dict[tuple[str, int], tuple[str, ...]] = {}
        self._template_errors: dict[tuple[str, int], str | None] = {}
        self._published_template_attributes: dict[
            tuple[str, int],
            dict[str, object],
        ] = {}
        self._configure_mqtt()
        self.listen_event(
            self._handle_notification_action,
            "mobile_app_notification_action",
        )
        for user in self.users:
            self.run_daily(self._midnight_rollover, time(), user=user)
        # Defer arming so initialize can finish before run_in(0) callbacks save.
        self.run_in(self._restore_reminders_callback, 1)
        self.log("Habit tracker initialized for %s", ", ".join(self.users))

    def _restore_reminders_callback(self, _kwargs: dict[str, Any]) -> None:
        self._restore_reminders()
        self._restore_templates()
        self.store.save()

    def _restore_templates(self) -> None:
        """Re-subscribe every event-driven slot and evaluate it once."""
        for user, data in self.store.data.users.items():
            for slot, config in data.habits.items():
                self._rebuild_template_listeners(user, slot)
                if config.configured and config.completion_mode.is_event_driven:
                    if (
                        config.completion_mode.is_duration_based
                        and (progress := data.template_progress.get(slot)) is not None
                    ):
                        # Banked summed time is trusted, but an unobserved
                        # in-flight interval across restart is not.
                        progress.truthy_since = None
                    self._schedule_evaluation(user, slot)

    def terminate(self) -> None:
        """Persist, mark unavailable, and disconnect cleanly."""
        with suppress(AttributeError):
            self.reminders.cancel_all()
        with suppress(AttributeError):
            self.templates.cancel_all()
        with suppress(AttributeError, OSError):
            self.store.save()
        if self.mqtt is not None:
            self.mqtt.disconnect()

    def _configure_mqtt(self) -> None:
        host = self.args.get("mqtt_host")
        if not isinstance(host, str) or not host:
            self.error("mqtt_host is required")
            return
        settings = MqttSettings(
            host=host,
            port=int(self.args.get("mqtt_port", 1883)),
            username=_optional_string(self.args.get("mqtt_username")),
            password=_optional_string(self.args.get("mqtt_password")),
            discovery_prefix=str(
                self.args.get("mqtt_discovery_prefix", "homeassistant"),
            ).strip("/"),
            base_topic=str(
                self.args.get("mqtt_base_topic", "appdaemon/habits"),
            ).strip("/"),
            qos=int(self.args.get("mqtt_qos", 0)),
        )
        self.mqtt = HabitMqtt(
            settings,
            on_command=self._handle_mqtt_command,
            dispatch=self._dispatch_from_mqtt,
            on_connect=self._publish_all,
            log=self.log,
        )
        try:
            self.mqtt.connect()
        except Exception as error:
            self.error("Unable to connect habit MQTT client: %s", error)
            with suppress(Exception):
                self.mqtt.disconnect()
            self.mqtt = None

    def _dispatch_from_mqtt(
        self,
        callback: Any,
        topic: str,
        payload: str,
    ) -> None:
        self.run_in(
            self._run_dispatched,
            0,
            dispatched_callback=callback,
            topic=topic,
            payload=payload,
        )

    def _run_dispatched(self, kwargs: dict[str, Any]) -> None:
        callback = kwargs["dispatched_callback"]
        callback(str(kwargs["topic"]), str(kwargs["payload"]))

    def _publish_all(self) -> None:
        if self.mqtt is None:
            return
        self.mqtt.publish_mood_discovery(self.users)
        for user in self.users:
            for retired_slot in self._startup_retired[user]:
                self.mqtt.retire_slot(user, retired_slot)
            for config in self.store.data.users[user].habits.values():
                self.mqtt.publish_slot(user, config)
                self._publish_habit_state(user, config.slot)
                self._publish_next_reminder(user, config.slot)
            self._publish_mood_state(user)
            self._publish_habit_type_counts(user)

    def _handle_mqtt_command(self, topic: str, payload: str) -> None:
        if self.mqtt is None:
            return
        prefix = f"{self.mqtt.settings.base_topic}/"
        if not topic.startswith(prefix) or not topic.endswith("/set"):
            return
        parts = topic[len(prefix) :].split("/")
        try:
            if len(parts) == SLOT_TOPIC_PARTS and parts[1] == "mood":
                self._update_mood(parts[0], parts[2], payload)
            elif len(parts) == SLOT_TOPIC_PARTS:
                self._update_habit(parts[0], int(parts[1]), parts[2], payload)
        except (KeyError, TypeError, ValueError) as error:
            self.error("Rejected MQTT command %s=%r: %s", topic, payload, error)

    def _update_habit(  # noqa: C901, PLR0912, PLR0915
        self,
        user: str,
        slot: int,
        key: str,
        payload: str,
    ) -> None:
        data = self.store.data.users[user]
        config = data.habits[slot]
        old_name = config.name
        old_type = config.habit_type
        old_reminder_time = config.reminder_time
        # Fields are mutated in place, but cross-field rules are only checked
        # afterwards. Keep a snapshot so a rejected command cannot leave the
        # in-memory config in a state a later save would persist.
        snapshot = config.to_dict()
        if key == "name":
            new_name = payload.strip()
            if len(new_name) > MAX_NAME_LENGTH:
                raise ValueError("habit name is too long")
            config.name = new_name
            if old_name.strip() and not new_name:
                data.completions.pop(slot, None)
                self._clear_pending(user, slot)
        elif key == "type":
            config.habit_type = HabitType(payload)
        elif key == "reminder_time":
            time.fromisoformat(payload)
            config.reminder_time = payload
        elif key == "next_reminder":
            self._override_next_reminder(user, slot, payload)
            return
        elif key == "repeat_count":
            config.repeat_count = _bounded_int(payload, 0, 100)
        elif key == "repeat_interval":
            config.repeat_interval_minutes = _bounded_int(payload, 1, 1440)
        elif key == "streak_min_days":
            config.streak_min_days_per_week = _bounded_int(payload, 1, 7)
        elif key == "ai":
            config.ai_enabled = _mqtt_bool(payload)
        elif key == "end_of_day_reminder":
            config.end_of_day_reminder_enabled = _mqtt_bool(payload)
        elif key in {"icon_on", "icon_active", "icon_off", "icon_zero"}:
            icon = payload.strip()
            if len(icon) > MAX_NAME_LENGTH:
                raise ValueError("habit icon is too long")
            setattr(config, key, icon)
        elif key == "completion_mode":
            config.completion_mode = CompletionMode(payload.strip())
        elif key == "completion_template":
            template = payload.strip()
            if len(template) > MAX_TEMPLATE_LENGTH:
                raise ValueError("completion template is too long")
            config.completion_template = template
        elif key == "completion_duration":
            config.completion_duration_minutes = _bounded_int(
                payload,
                1,
                MAX_DURATION_MINUTES,
            )
        elif key == "state":
            self._set_completion(user, slot, 1 if _mqtt_bool(payload) else 0)
            return
        elif key == "count":
            self._set_completion(user, slot, _bounded_int(payload, 0, 100000))
            return
        else:
            raise KeyError(key)
        try:
            config.__post_init__()
        except ValueError:
            for field_name, previous in snapshot.items():
                setattr(config, field_name, previous)
            raise
        counts_changed = config.name != old_name or config.habit_type is not old_type
        if counts_changed:
            self._clear_pending(user, slot)
        if (
            config.reminder_time != old_reminder_time
            and config.configured
            and (pending := data.pending_reminders.get(slot)) is not None
            and pending.next_index == 1
        ):
            self._seed_next_reminder(user, config, force=True)
        elif config.configured and slot not in data.pending_reminders:
            self._seed_next_reminder(user, config)
        if key in {
            "completion_duration",
            "completion_mode",
            "completion_template",
            "name",
        }:
            self._rebuild_template_listeners(user, slot)
            self._schedule_evaluation(user, slot)
        # Name changes can configure or delete a slot, so reconcile the derived
        # timer after every configuration edit rather than only after toggles.
        self._schedule_end_of_day_reminder(user, config)
        added_spare, retired = self._normalize_user_slots(user)
        config = data.habits.get(slot)
        self.store.save()
        if self.mqtt is not None:
            if config is not None:
                self.mqtt.publish_slot(user, config)
                self._publish_habit_state(user, slot)
                self._publish_next_reminder(user, slot)
            if added_spare is not None and added_spare != slot:
                spare = data.habits[added_spare]
                self.mqtt.publish_slot(user, spare)
                self._publish_habit_state(user, added_spare)
                self._publish_next_reminder(user, added_spare)
            for retired_slot in retired:
                self._clear_pending(user, retired_slot)
                self.mqtt.retire_slot(user, retired_slot)
            if counts_changed:
                self._publish_habit_type_counts(user)

    def _update_mood(self, user: str, key: str, payload: str) -> None:
        data = self.store.data.users[user]
        if key == "mood_today":
            self._apply_mood_today(user, data, payload)
        elif key == "mood_note":
            data.mood_note = payload[:255]
        elif key == "mood_reminder_time":
            self._apply_mood_reminder_time(user, data, payload)
        elif key == "mood_reminders":
            self._apply_mood_reminders_enabled(user, data, enabled=_mqtt_bool(payload))
            return
        elif key == "mood_next_reminder":
            self._override_mood_next_reminder(user, payload)
            return
        elif key == "mood_repeat_count":
            data.mood_repeat_count = _bounded_int(payload, 0, 100)
        elif key == "mood_repeat_interval":
            data.mood_repeat_interval_minutes = _bounded_int(payload, 1, 1440)
        else:
            raise KeyError(key)
        data.__post_init__()
        self.store.save()
        self._publish_mood_state(user)

    def _apply_mood_today(self, user: str, data: UserData, payload: str) -> None:
        if payload not in MOOD_OPTIONS:
            raise ValueError("invalid mood option")
        today = self.datetime().date().isoformat()
        data.mood_today = payload
        if payload != "Not Set":
            data.mood_history[today] = payload
            self._clear_mood_pending(user)
            return
        data.mood_history.pop(today, None)
        self._seed_mood_reminder(user, force=True)

    def _apply_mood_reminder_time(
        self,
        user: str,
        data: UserData,
        payload: str,
    ) -> None:
        time.fromisoformat(payload)
        data.mood_reminder_time = payload
        pending = data.pending_mood_reminder
        if pending is not None and pending.next_index == 1:
            self._seed_mood_reminder(user, force=True)
        elif pending is None:
            self._seed_mood_reminder(user)

    def _apply_mood_reminders_enabled(
        self,
        user: str,
        data: UserData,
        *,
        enabled: bool,
    ) -> None:
        data.mood_reminders_enabled = enabled
        if enabled:
            self._seed_mood_reminder(user, force=True)
        else:
            self._clear_mood_pending(user)
        self.store.save()
        self._publish_mood_state(user)

    def _set_completion(self, user: str, slot: int, count: int) -> None:
        day = self.datetime().date().isoformat()
        values = self.store.data.users[user].completions.setdefault(slot, {})
        if count:
            values[day] = count
        else:
            values.pop(day, None)
        data = self.store.data.users[user]
        config = data.habits[slot]
        if count:
            self._clear_pending(user, slot)
            self.reminders.cancel_end_of_day(user, slot)
        elif config.configured:
            self._seed_next_reminder(user, config, force=True)
            self._schedule_end_of_day_reminder(user, config)
        if config.completion_mode.is_event_driven:
            self.templates.cancel_duration(user, slot)
            progress = self._template_progress(user, slot, day)
            progress.truthy_since = None
            progress.accumulated_seconds = 0
            if not count:
                # A manual un-tick must not be instantly undone by a template
                # that is still truthy, so stop evaluating this slot for today.
                progress.suppressed_day = day
        self.store.append_completion_log(user, slot, day, count)
        self.store.save()
        self._publish_habit_state(user, slot)
        self._publish_next_reminder(user, slot)

    def _publish_habit_state(self, user: str, slot: int) -> None:
        if self.mqtt is None:
            return
        data = self.store.data.users[user]
        config = data.habits[slot]
        today = self.datetime().date()
        today_count = data.completions.get(slot, {}).get(today.isoformat(), 0)
        prefix = self.mqtt.topic(f"{user}/{slot}")
        template_attributes = self._template_attributes(user, slot)
        self.mqtt.publish(f"{prefix}/state/state", "ON" if today_count else "OFF")
        self.mqtt.publish(f"{prefix}/count/state", str(today_count))
        stats = calculate_streak(
            data.completions.get(slot, {}),
            today=today,
            min_days_per_week=config.streak_min_days_per_week,
        )
        self.mqtt.publish(
            f"{prefix}/streak/state",
            str(stats.streak),
        )
        self.mqtt.publish(
            f"{prefix}/streak/attributes",
            {
                **self._slot_attributes(config),
                "days_since_completion": stats.days_since_completion,
                "completion_rate_28_days": stats.completion_rate_28_days,
            },
        )
        state_key = "state" if config.habit_type is HabitType.BINARY else "count"
        self.mqtt.publish(
            f"{prefix}/{state_key}/attributes",
            {
                **self._slot_attributes(config),
                "icon": (
                    config.icon_on
                    if config.habit_type is HabitType.BINARY and today_count
                    else config.icon_off
                    if config.habit_type is HabitType.BINARY
                    else config.icon_active
                    if today_count
                    else config.icon_zero
                ),
            },
        )
        self.mqtt.publish(
            f"{prefix}/name/attributes",
            {
                **self._slot_attributes(config),
                **template_attributes,
            },
        )
        self._published_template_attributes[(user, slot)] = template_attributes

    def _publish_mood_state(self, user: str) -> None:
        if self.mqtt is None:
            return
        data = self.store.data.users[user]
        prefix = self.mqtt.topic(f"{user}/mood")
        self.mqtt.publish(f"{prefix}/mood_today/state", data.mood_today)
        self.mqtt.publish(f"{prefix}/mood_note/state", data.mood_note)
        self.mqtt.publish(
            f"{prefix}/mood_streak/state",
            str(
                calculate_mood_streak(
                    data.mood_history,
                    today=self.datetime().date(),
                ),
            ),
        )
        self.mqtt.publish(f"{prefix}/mood_reminder_time/state", data.mood_reminder_time)
        self.mqtt.publish(
            f"{prefix}/mood_reminders/state",
            "ON" if data.mood_reminders_enabled else "OFF",
        )
        self.mqtt.publish(
            f"{prefix}/mood_repeat_count/state",
            str(data.mood_repeat_count),
        )
        self.mqtt.publish(
            f"{prefix}/mood_repeat_interval/state",
            str(data.mood_repeat_interval_minutes),
        )
        self._publish_mood_next_reminder(user)

    def _publish_habit_type_counts(self, user: str) -> None:
        if self.mqtt is None:
            return
        binary = 0
        countable = 0
        for config in self.store.data.users[user].habits.values():
            if not config.configured:
                continue
            if config.habit_type is HabitType.BINARY:
                binary += 1
            elif config.habit_type is HabitType.COUNTABLE:
                countable += 1
        self.mqtt.publish(
            self.mqtt.topic(f"{user}/habits_binary_count/state"),
            str(binary),
        )
        self.mqtt.publish(
            self.mqtt.topic(f"{user}/habits_countable_count/state"),
            str(countable),
        )

    def _restore_reminders(self) -> None:
        if not self.reminders_enabled:
            return
        for user, data in self.store.data.users.items():
            changed = self._restore_habit_reminders(user, data)
            changed = self._restore_mood_reminder(user, data) or changed
            if changed:
                self.store.save()
            for slot in data.habits:
                self._publish_next_reminder(user, slot)
            self._publish_mood_next_reminder(user)

    def _restore_habit_reminders(self, user: str, data: UserData) -> bool:
        changed = False
        for slot, config in list(data.habits.items()):
            pending = data.pending_reminders.get(slot)
            if not config.configured or self._is_complete_today(user, slot):
                self.reminders.cancel_end_of_day(user, slot)
                if pending is not None:
                    self._clear_pending(user, slot)
                    changed = True
                continue
            self._schedule_end_of_day_reminder(user, config)
            if pending is None:
                self._seed_next_reminder(user, config)
                changed = True
                continue
            self._arm_pending(user, slot, pending)
        return changed

    def _schedule_end_of_day_reminder(
        self,
        user: str,
        config: HabitConfig,
    ) -> None:
        """Arm today's opt-in 23:55 reminder independently of the repeat chain."""
        if (
            not self.reminders_enabled
            or not config.configured
            or not config.end_of_day_reminder_enabled
            or self._is_complete_today(user, config.slot)
        ):
            self.reminders.cancel_end_of_day(user, config.slot)
            return
        now = self._aware_now()
        fire_at = end_of_day_reminder_fire_at(
            now,
            last_sent_day=self.store.data.users[user].end_of_day_reminder_sent_days.get(
                config.slot,
            ),
        )
        if fire_at is None:
            self.reminders.cancel_end_of_day(user, config.slot)
            return
        self.reminders.schedule_end_of_day(
            user,
            config.slot,
            fire_at=fire_at,
            now=now,
        )

    def _restore_mood_reminder(self, user: str, data: UserData) -> bool:
        if not data.mood_reminders_enabled or data.mood_today != "Not Set":
            if data.pending_mood_reminder is None:
                return False
            self._clear_mood_pending(user)
            return True
        if data.pending_mood_reminder is None:
            self._seed_mood_reminder(user)
            return True
        self._arm_mood_pending(user, data.pending_mood_reminder)
        return False

    def _seed_next_reminder(
        self,
        user: str,
        config: HabitConfig,
        *,
        force: bool = False,
    ) -> None:
        if not self.reminders_enabled or not config.configured:
            return
        if self._is_complete_today(user, config.slot):
            self._clear_pending(user, config.slot)
            return
        data = self.store.data.users[user]
        if not force and config.slot in data.pending_reminders:
            return
        now = self._aware_now()
        fire_at = datetime.combine(
            now.date(),
            time.fromisoformat(config.reminder_time),
            tzinfo=now.tzinfo,
        )
        fire_at = max(fire_at, now)
        pending = PendingReminder(
            fire_at=self._utc_isoformat(fire_at),
            next_index=1,
            final_index=config.repeat_count + 1,
        )
        data.pending_reminders[config.slot] = pending
        self._arm_pending(user, config.slot, pending)
        self._publish_next_reminder(user, config.slot)

    def _seed_mood_reminder(self, user: str, *, force: bool = False) -> None:
        if not self.reminders_enabled:
            return
        data = self.store.data.users[user]
        if not data.mood_reminders_enabled:
            self._clear_mood_pending(user)
            return
        if data.mood_today != "Not Set":
            self._clear_mood_pending(user)
            return
        if not force and data.pending_mood_reminder is not None:
            return
        now = self._aware_now()
        fire_at = datetime.combine(
            now.date(),
            time.fromisoformat(data.mood_reminder_time),
            tzinfo=now.tzinfo,
        )
        fire_at = max(fire_at, now)
        pending = PendingReminder(
            fire_at=self._utc_isoformat(fire_at),
            next_index=1,
            final_index=data.mood_repeat_count + 1,
        )
        data.pending_mood_reminder = pending
        self._arm_mood_pending(user, pending)
        self._publish_mood_next_reminder(user)

    def _override_next_reminder(self, user: str, slot: int, payload: str) -> None:
        data = self.store.data.users[user]
        config = data.habits[slot]
        if not payload.strip() or payload.strip() == "None":
            self._clear_pending(user, slot)
            self.store.save()
            self._publish_next_reminder(user, slot)
            return
        if not config.configured:
            raise ValueError("habit is not configured")
        fire_at = self._parse_fire_at(payload)
        pending = data.pending_reminders.get(slot)
        if (
            pending is not None
            and abs(
                (fire_at - self._parse_fire_at(pending.fire_at)).total_seconds(),
            )
            < NEXT_REMINDER_ECHO_TOLERANCE_SECONDS
        ):
            return
        if pending is None:
            pending = PendingReminder(
                fire_at=self._utc_isoformat(fire_at),
                next_index=1,
                final_index=config.repeat_count + 1,
            )
        else:
            pending = PendingReminder(
                fire_at=self._utc_isoformat(fire_at),
                next_index=pending.next_index,
                final_index=pending.final_index,
            )
        data.pending_reminders[slot] = pending
        self._arm_pending(user, slot, pending)
        self.store.save()
        self._publish_next_reminder(user, slot)

    def _override_mood_next_reminder(self, user: str, payload: str) -> None:
        data = self.store.data.users[user]
        if not payload.strip() or payload.strip() == "None":
            self._clear_mood_pending(user)
            self.store.save()
            self._publish_mood_state(user)
            return
        if not data.mood_reminders_enabled:
            raise ValueError("mood reminders are disabled")
        fire_at = self._parse_fire_at(payload)
        pending = data.pending_mood_reminder
        if (
            pending is not None
            and abs(
                (fire_at - self._parse_fire_at(pending.fire_at)).total_seconds(),
            )
            < NEXT_REMINDER_ECHO_TOLERANCE_SECONDS
        ):
            return
        if pending is None:
            pending = PendingReminder(
                fire_at=self._utc_isoformat(fire_at),
                next_index=1,
                final_index=data.mood_repeat_count + 1,
            )
        else:
            pending = PendingReminder(
                fire_at=self._utc_isoformat(fire_at),
                next_index=pending.next_index,
                final_index=pending.final_index,
            )
        data.pending_mood_reminder = pending
        self._arm_mood_pending(user, pending)
        self.store.save()
        self._publish_mood_state(user)

    def _arm_pending(self, user: str, slot: int, pending: PendingReminder) -> None:
        if not self.reminders_enabled:
            self.reminders.cancel(user, slot)
            return
        now = self._aware_now()
        fire_at = self._parse_fire_at(pending.fire_at)
        self.reminders.schedule_at(
            user,
            slot,
            fire_at=fire_at,
            reminder_index=pending.next_index,
            final_index=pending.final_index,
            now=now,
        )

    def _arm_mood_pending(self, user: str, pending: PendingReminder) -> None:
        if (
            not self.reminders_enabled
            or not self.store.data.users[user].mood_reminders_enabled
        ):
            self.reminders.cancel_mood(user)
            return
        now = self._aware_now()
        fire_at = self._parse_fire_at(pending.fire_at)
        self.reminders.schedule_mood(
            user,
            fire_at=fire_at,
            reminder_index=pending.next_index,
            final_index=pending.final_index,
            now=now,
        )

    def _clear_pending(self, user: str, slot: int) -> None:
        self.reminders.cancel(user, slot)
        self.store.data.users[user].pending_reminders.pop(slot, None)
        self._publish_next_reminder(user, slot)

    def _clear_mood_pending(self, user: str) -> None:
        self.reminders.cancel_mood(user)
        self.store.data.users[user].pending_mood_reminder = None
        self._publish_mood_next_reminder(user)

    def _publish_next_reminder(self, user: str, slot: int) -> None:
        if self.mqtt is None:
            return
        pending = self.store.data.users[user].pending_reminders.get(slot)
        payload = (
            "None"
            if pending is None
            else self._utc_isoformat(self._parse_fire_at(pending.fire_at))
        )
        self.mqtt.publish(
            f"{self.mqtt.topic(f'{user}/{slot}')}/next_reminder/state",
            payload,
        )

    def _publish_mood_next_reminder(self, user: str) -> None:
        if self.mqtt is None:
            return
        pending = self.store.data.users[user].pending_mood_reminder
        payload = (
            "None"
            if pending is None
            else self._utc_isoformat(self._parse_fire_at(pending.fire_at))
        )
        self.mqtt.publish(
            f"{self.mqtt.topic(f'{user}/mood')}/mood_next_reminder/state",
            payload,
        )

    def _rebuild_template_listeners(self, user: str, slot: int) -> None:
        """Re-subscribe a slot to the entities its template actually references."""
        self.templates.cancel_duration(user, slot)
        config = self.store.data.users[user].habits.get(slot)
        if (
            config is None
            or not config.configured
            or not config.completion_mode.is_event_driven
        ):
            self.templates.remove(user, slot)
            self._watched_entities.pop((user, slot), None)
            self._template_errors.pop((user, slot), None)
            return
        candidates = extract_candidate_entities(config.completion_template)
        # The pattern cannot distinguish an entity from an attribute access, so
        # keep only candidates Home Assistant actually knows about.
        entities = [name for name in candidates if self.get_state(name) is not None]
        if not entities:
            self.error(
                "Habit template for %s slot %s references no known entities, so it "
                "will never re-evaluate: %r",
                user,
                slot,
                config.completion_template,
            )
        if config.completion_mode.is_duration_based:
            entities.append(TIME_TICK_ENTITY)
        self._watched_entities[(user, slot)] = self.templates.watch(
            user,
            slot,
            entities,
        )

    def _template_dependency_changed(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        del entity, attribute, old, new
        self._schedule_evaluation(str(kwargs["user"]), int(kwargs["slot"]))

    def _schedule_evaluation(self, user: str, slot: int) -> None:
        self.templates.schedule(user, slot, self.template_eval_debounce_seconds)

    def _template_evaluation_callback(self, kwargs: dict[str, Any]) -> None:
        user = str(kwargs["user"])
        slot = int(kwargs["slot"])
        self.templates.release(user, slot)
        self._evaluate_template(user, slot)

    def _duration_elapsed_callback(self, kwargs: dict[str, Any]) -> None:
        """Re-evaluate a slot when its required truthy duration has elapsed."""
        user = str(kwargs["user"])
        slot = int(kwargs["slot"])
        self.templates.release_duration(user, slot)
        self._evaluate_template(user, slot)

    def _template_progress(
        self,
        user: str,
        slot: int,
        today: str,
    ) -> TemplateProgress:
        """Return today's progress record, resetting a stale one."""
        data = self.store.data.users[user]
        progress = data.template_progress.get(slot)
        if progress is None or progress.day != today:
            progress = TemplateProgress(day=today)
            data.template_progress[slot] = progress
        return progress

    def _render_truthy(self, user: str, slot: int, template: str) -> bool:
        """Render a template, treating any failure as falsy."""
        key = (user, slot)
        try:
            rendered = self.render_template(template)
        except Exception as error:
            self.error("Habit template failed for %s slot %s: %s", user, slot, error)
            self._template_errors[key] = str(error)
            return False
        truthy = coerce_truthy(rendered)
        if truthy is None:
            self.error(
                "Habit template for %s slot %s returned an unusable value: %r",
                user,
                slot,
                rendered,
            )
            self._template_errors[key] = f"unusable template result: {rendered!r}"
            return False
        self._template_errors[key] = None
        return truthy

    def _evaluate_template(self, user: str, slot: int) -> None:
        """Evaluate one slot's completion template and act on the result."""
        if not self.template_evaluation_enabled:
            return
        data = self.store.data.users[user]
        config = data.habits.get(slot)
        if config is None or not config.configured:
            return
        if not config.completion_mode.is_event_driven:
            if data.template_progress.pop(slot, None) is not None:
                self.store.save()
            return
        if not config.completion_template.strip():
            return
        today = self.datetime().date().isoformat()
        previous = data.template_progress.get(slot)
        progress_before = (
            previous.to_dict() if previous is not None and previous.day == today else None
        )
        progress = self._template_progress(user, slot, today)
        if progress.suppressed_day == today:
            self.templates.cancel_duration(user, slot)
            self._save_template_progress_if_changed(progress, progress_before)
            self._publish_template_attributes_if_changed(user, slot)
            return
        if self._is_complete_today(user, slot):
            self.templates.cancel_duration(user, slot)
            progress.truthy_since = None
            progress.accumulated_seconds = 0
            self._save_template_progress_if_changed(progress, progress_before)
            self._publish_template_attributes_if_changed(user, slot)
            return
        now = self._aware_now()
        now_truthy = self._render_truthy(user, slot, config.completion_template)
        if config.completion_mode is CompletionMode.INSTANT:
            completed = self._apply_instant_mode(
                user,
                slot,
                progress,
                now_truthy=now_truthy,
                now=now,
            )
        else:
            completed = self._apply_duration_mode(
                user,
                slot,
                config,
                progress,
                now_truthy=now_truthy,
                now=now,
            )
        if completed:
            self.log("Habit template completed %s slot %s", user, slot)
            self._set_completion(user, slot, 1)
        else:
            self._save_template_progress_if_changed(progress, progress_before)
            self._publish_template_attributes_if_changed(user, slot)

    def _apply_instant_mode(
        self,
        user: str,
        slot: int,
        progress: TemplateProgress,
        *,
        now_truthy: bool,
        now: datetime,
    ) -> bool:
        """Apply rising-edge completion for an instant-mode slot."""
        self.templates.cancel_duration(user, slot)
        was_truthy = progress.is_truthy
        progress.truthy_since = self._utc_isoformat(now) if now_truthy else None
        return now_truthy and not was_truthy

    def _apply_duration_mode(
        self,
        user: str,
        slot: int,
        config: HabitConfig,
        progress: TemplateProgress,
        *,
        now_truthy: bool,
        now: datetime,
    ) -> bool:
        """Bank elapsed truthy time and arm the remaining duration."""
        elapsed = self._elapsed_truthy_seconds(progress, now)
        if not now_truthy:
            if progress.is_truthy and config.completion_mode is CompletionMode.SUMMED:
                progress.accumulated_seconds += elapsed
            progress.truthy_since = None
            self.templates.cancel_duration(user, slot)
            return False
        if not progress.is_truthy:
            progress.truthy_since = self._utc_isoformat(now)
            elapsed = 0
        counted = elapsed
        if config.completion_mode is CompletionMode.SUMMED:
            counted += progress.accumulated_seconds
        remaining = config.completion_duration_minutes * 60 - counted
        if remaining <= 0:
            self.templates.cancel_duration(user, slot)
            return True
        self.templates.arm_duration(user, slot, remaining)
        return False

    def _elapsed_truthy_seconds(
        self,
        progress: TemplateProgress,
        now: datetime,
    ) -> int:
        """Return whole elapsed truthy seconds, clamped against clock skew."""
        if progress.truthy_since is None:
            return 0
        truthy_since = self._parse_fire_at(progress.truthy_since)
        return max(0, int((now - truthy_since).total_seconds()))

    def _save_template_progress_if_changed(
        self,
        progress: TemplateProgress,
        previous: dict[str, Any] | None,
    ) -> None:
        """Persist template progress only when its durable state changed."""
        if progress.to_dict() != previous:
            self.store.save()

    def _publish_template_attributes_if_changed(self, user: str, slot: int) -> None:
        """Republish a slot only when its template attributes changed."""
        attributes = self._template_attributes(user, slot)
        if self._published_template_attributes.get((user, slot)) != attributes:
            self._publish_habit_state(user, slot)

    def _template_attributes(self, user: str, slot: int) -> dict[str, object]:
        """Build the observable state of a slot's completion template."""
        data = self.store.data.users[user]
        config = data.habits[slot]
        today = self.datetime().date().isoformat()
        progress = data.template_progress.get(slot)
        if progress is not None and progress.day != today:
            progress = None
        attributes: dict[str, object] = {
            "completion_mode": config.completion_mode,
            "condition": progress.is_truthy if progress is not None else False,
            "template_error": self._template_errors.get((user, slot)),
            "watched_entities": list(self._watched_entities.get((user, slot), ())),
        }
        if config.completion_mode.is_duration_based:
            seconds = 0
            if progress is not None:
                if config.completion_mode is CompletionMode.SUMMED:
                    seconds = progress.accumulated_seconds
                seconds += self._elapsed_truthy_seconds(progress, self._aware_now())
            attributes.update(
                {
                    "condition_minutes": round(seconds / 60, 2),
                    "condition_target_minutes": config.completion_duration_minutes,
                },
            )
        return attributes

    def _is_complete_today(self, user: str, slot: int) -> bool:
        return (
            self.store.data.users[user]
            .completions.get(slot, {})
            .get(self.datetime().date().isoformat(), 0)
            > 0
        )

    def _parse_fire_at(self, value: str) -> datetime:
        fire_at = datetime.fromisoformat(value)
        # HA MQTT datetime commands are UTC; naive payloads must not be treated
        # as local or schedules jump an hour early and re-fire immediately.
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=UTC)
        return fire_at.astimezone(self._aware_now().tzinfo)

    @staticmethod
    def _utc_isoformat(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    def _aware_now(self) -> datetime:
        now = self.datetime()
        if now.tzinfo is not None:
            return now
        return now.replace(tzinfo=datetime.now().astimezone().tzinfo)

    def _reminder_callback(self, kwargs: dict[str, Any]) -> None:
        user = str(kwargs["user"])
        if kwargs.get("kind") == "mood":
            self.reminders.release_mood(user)
            self._send_mood_reminder(
                user,
                int(kwargs["reminder_index"]),
                final_index=int(
                    kwargs.get(
                        "final_index",
                        self.store.data.users[user].mood_repeat_count + 1,
                    ),
                ),
            )
            return
        slot = int(kwargs["slot"])
        if kwargs.get("kind") == "end_of_day":
            self.reminders.release_end_of_day(user, slot)
            self._send_end_of_day_reminder(user, slot)
            return
        self.reminders.release(user, slot)
        self._send_reminder(
            user,
            slot,
            int(kwargs["reminder_index"]),
            final_index=int(
                kwargs.get(
                    "final_index",
                    self.store.data.users[user].habits[slot].repeat_count + 1,
                ),
            ),
        )

    def _send_reminder(
        self,
        user: str,
        slot: int,
        reminder_index: int,
        *,
        final_index: int | None = None,
    ) -> None:
        if not self.reminders_enabled:
            return
        data = self.store.data.users[user]
        config = data.habits.get(slot)
        if config is None or not config.configured:
            self._clear_pending(user, slot)
            self.store.save()
            return
        if self._is_complete_today(user, slot):
            self._clear_pending(user, slot)
            self.store.save()
            return
        # Arm the next fire before waiting on AI so a slow model cannot push the
        # schedule out. Only send one notification: AI text when available,
        # otherwise the deterministic fallback.
        now = self._aware_now()
        last_index = final_index or config.repeat_count + 1
        next_index = reminder_index + 1
        if next_index <= last_index and repeat_fits_before_midnight(
            now,
            config.repeat_interval_minutes,
        ):
            fire_at = now + timedelta(minutes=config.repeat_interval_minutes)
            pending = PendingReminder(
                fire_at=self._utc_isoformat(fire_at),
                next_index=next_index,
                final_index=last_index,
            )
            data.pending_reminders[slot] = pending
            self._arm_pending(user, slot, pending)
        else:
            self._clear_pending(user, slot)
        self.store.save()
        self._publish_next_reminder(user, slot)

        message = self._fallback_message(config)
        if config.ai_enabled:
            message = self._ai_message(user, config) or message
        self.log(
            "Sending habit reminder for %s slot %s (attempt %s)",
            user,
            slot,
            reminder_index,
        )
        self._notify_habit(user, slot, config, message)

    def _send_end_of_day_reminder(self, user: str, slot: int) -> None:
        """Send the opt-in final check without changing the regular reminder chain."""
        if not self.reminders_enabled:
            return
        data = self.store.data.users[user]
        config = data.habits.get(slot)
        today = self._aware_now().date().isoformat()
        if (
            config is None
            or not config.configured
            or not config.end_of_day_reminder_enabled
            or self._is_complete_today(user, slot)
            or data.end_of_day_reminder_sent_days.get(slot) == today
        ):
            return
        # Persist before notifying so a restart or configuration edit cannot
        # re-arm another final check after this one has fired.
        data.end_of_day_reminder_sent_days[slot] = today
        self.store.save()
        message = (
            f"If you've already done {config.name}, mark it complete before midnight "
            "to keep your streak."
        )
        self.log("Sending end-of-day habit reminder for %s slot %s", user, slot)
        self._notify_habit(user, slot, config, message)

    def _notify_habit(
        self,
        user: str,
        slot: int,
        config: HabitConfig,
        message: str,
    ) -> None:
        action = (
            f"MARK_HABIT_AS_COMPLETE__{user.upper()}__{slot}"
            if config.habit_type is HabitType.BINARY
            else f"INCREMENT_HABIT__{user.upper()}__{slot}"
        )
        streak = calculate_streak(
            self.store.data.users[user].completions.get(slot, {}),
            today=self._aware_now().date(),
            min_days_per_week=config.streak_min_days_per_week,
        ).streak
        title = f"{config.name} · {streak}-day streak"
        user_config = self._user_config(user)
        try:
            self.call_service(
                "script/turn_on",
                entity_id=user_config["notify_script"],
                variables={
                    "title": title,
                    "message": message,
                    "notification_id": f"{user}_habit_{slot}_reminder",
                    "mobile_notification_icon": "mdi:checkbox-marked-circle-outline",
                    "url": user_config["dashboard_url"],
                    "actions": json.dumps(
                        [
                            {
                                "action": action,
                                "title": (
                                    "Mark as Complete"
                                    if config.habit_type is HabitType.BINARY
                                    else "Increment"
                                ),
                            },
                        ],
                    ),
                },
            )
        except Exception as error:
            self.error(
                "Habit reminder notification failed for %s slot %s: %s",
                user,
                slot,
                error,
            )

    def _send_mood_reminder(
        self,
        user: str,
        reminder_index: int,
        *,
        final_index: int | None = None,
    ) -> None:
        if not self.reminders_enabled:
            return
        data = self.store.data.users[user]
        if not data.mood_reminders_enabled:
            self._clear_mood_pending(user)
            self.store.save()
            return
        if data.mood_today != "Not Set":
            self._clear_mood_pending(user)
            self.store.save()
            return
        now = self._aware_now()
        last_index = final_index or data.mood_repeat_count + 1
        next_index = reminder_index + 1
        if next_index <= last_index and repeat_fits_before_midnight(
            now,
            data.mood_repeat_interval_minutes,
        ):
            fire_at = now + timedelta(minutes=data.mood_repeat_interval_minutes)
            pending = PendingReminder(
                fire_at=self._utc_isoformat(fire_at),
                next_index=next_index,
                final_index=last_index,
            )
            data.pending_mood_reminder = pending
            self._arm_mood_pending(user, pending)
        else:
            self._clear_mood_pending(user)
        self.store.save()
        self._publish_mood_next_reminder(user)

        message = self._ai_mood_message(user) or MOOD_FALLBACK_MESSAGE
        self.log("Sending mood reminder for %s (attempt %s)", user, reminder_index)
        self._notify_mood(user, message)

    def _notify_mood(self, user: str, message: str) -> None:
        user_config = self._user_config(user)
        try:
            self.call_service(
                "script/turn_on",
                entity_id=user_config["notify_script"],
                variables={
                    "title": "Mood",
                    "message": message,
                    "notification_id": f"{user}_mood_reminder",
                    "mobile_notification_icon": "mdi:emoticon-outline",
                    "url": user_config["dashboard_url"],
                    "actions": "[]",
                },
            )
        except Exception as error:
            self.error("Mood reminder notification failed for %s: %s", user, error)

    def _ai_message(self, user: str, config: HabitConfig) -> str | None:
        return self._generate_ai_message(
            task_name=(f"habit reminder {user}_habit_{config.habit_type}_{config.slot}"),
            instructions=AI_INSTRUCTIONS,
            context=self._ai_context(user, config),
            kind="habit",
        )

    def _ai_mood_message(self, user: str) -> str | None:
        return self._generate_ai_message(
            task_name=f"mood reminder {user}",
            instructions=MOOD_AI_INSTRUCTIONS,
            context=self._ai_mood_context(user),
            kind="mood",
        )

    def _generate_ai_message(
        self,
        *,
        task_name: str,
        instructions: str,
        context: str,
        kind: str,
    ) -> str | None:
        try:
            # Pass entity_id inside service_data — AppDaemon's top-level entity_id
            # kwarg is moved into target and HA rejects that shape for ai_task.
            response = self.call_service(
                "ai_task/generate_data",
                service_data={
                    "entity_id": self.args["ai_task_entity"],
                    "task_name": task_name,
                    "instructions": f"{instructions}\n\nContext:\n{context}",
                },
                return_response=True,
                hass_timeout=180,
                timeout=180,
            )
        except Exception as error:
            self.error("AI %s reminder failed: %s", kind, error)
            return None
        message = _ai_response_text(response)
        if message is None:
            self.error(
                "AI %s reminder returned no usable text (response=%r)",
                kind,
                response,
            )
        return message

    def _ai_context(self, user: str, config: HabitConfig) -> str:
        data = self.store.data.users[user]
        user_config = self._user_config(user)
        now = self._aware_now()
        stats = calculate_streak(
            data.completions.get(config.slot, {}),
            today=now.date(),
            min_days_per_week=config.streak_min_days_per_week,
        )
        pairs: list[tuple[str, object]] = [
            ("Habit name", config.name),
            ("Habit type", config.habit_type),
            ("Current streak days", stats.streak),
            ("Days since completion", stats.days_since_completion),
            ("28-day completion rate", stats.completion_rate_28_days),
            ("Mood today", data.mood_today),
            ("Mood note", data.mood_note),
            ("Location category", self._location_category(user_config)),
            ("Calendar availability", self._calendar_availability(user, now)),
            (
                "Workday",
                "yes"
                if self.get_state(str(user_config["workday_entity"])) == "on"
                else "no",
            ),
            (
                "Current physical activity",
                self._state_value(str(user_config["activity_entity"])),
            ),
            ("Weather", self._weather_summary(str(user_config["weather_entity"]))),
            ("Now", _local_now_label(now)),
        ]
        return "\n".join(
            f"- {label}: {value}"
            for label, value in pairs
            if value not in INVALID_STATES and value not in {"Not Set", -1}
        )

    def _ai_mood_context(self, user: str) -> str:
        data = self.store.data.users[user]
        user_config = self._user_config(user)
        now = self._aware_now()
        pairs: list[tuple[str, object]] = [
            ("Mood today", data.mood_today),
            (
                "Mood streak days",
                calculate_mood_streak(data.mood_history, today=now.date()),
            ),
            ("Location category", self._location_category(user_config)),
            ("Calendar availability", self._calendar_availability(user, now)),
            (
                "Workday",
                "yes"
                if self.get_state(str(user_config["workday_entity"])) == "on"
                else "no",
            ),
            ("Now", _local_now_label(now)),
        ]
        return "\n".join(
            f"- {label}: {value}"
            for label, value in pairs
            if value not in INVALID_STATES and value not in {"Not Set", -1}
        )

    def _location_category(self, user_config: dict[str, Any]) -> str:
        person_entity = str(user_config["person_entity"])
        person_state = self.get_state(person_entity)
        all_state = self.get_state(person_entity, attribute="all")
        in_zones: list[str] = []
        if isinstance(all_state, dict):
            attributes = all_state.get("attributes")
            if isinstance(attributes, dict) and isinstance(
                attributes.get("in_zones"),
                list,
            ):
                in_zones = [str(zone) for zone in attributes["in_zones"]]
        labels = self._context_labels()
        for category, key in (
            ("home", "home"),
            ("work", "work"),
            ("family location", "family"),
        ):
            if set(in_zones) & set(self._label_entities(str(labels[key]))):
                return category
        if self.get_state(str(user_config["at_work_entity"])) == "on":
            return "work"
        if person_state == "home":
            return "home"
        if person_state == "not_home":
            return "away from known zones"
        return (
            "known but unclassified location"
            if person_state not in INVALID_STATES
            else "unknown"
        )

    def _calendar_availability(  # noqa: C901, PLR0911, PLR0912
        self,
        user: str,
        now: datetime,
    ) -> str:
        calendar_labels = self._context_labels()["calendar"]
        if not isinstance(calendar_labels, dict):
            return ""
        label = calendar_labels.get(user)
        entities = self._label_entities(label) if isinstance(label, str) else []
        if not entities:
            return ""
        try:
            response = self.call_service(
                "calendar/get_events",
                entity_id=entities,
                duration={"hours": 8},
                return_response=True,
            )
        except Exception as error:
            self.error("Calendar context failed for %s: %s", user, error)
            return ""
        payload = _service_result(response)
        if payload is None:
            return ""
        busy_until: datetime | None = None
        next_start: datetime | None = None
        for calendar in payload.values():
            if not isinstance(calendar, dict) or not isinstance(
                calendar.get("events"),
                list,
            ):
                continue
            for event in calendar["events"]:
                if not isinstance(event, dict):
                    continue
                start = _event_datetime(event.get("start"), now)
                end = _event_datetime(event.get("end"), now)
                if start is None or end is None:
                    continue
                if start <= now < end and (busy_until is None or end > busy_until):
                    busy_until = end
                elif start > now and (next_start is None or start < next_start):
                    next_start = start
        if busy_until is not None:
            minutes = round((busy_until - now).total_seconds() / 60)
            return f"in a meeting for the next {minutes} minutes"
        if next_start is not None:
            minutes = round((next_start - now).total_seconds() / 60)
            return f"free for the next {minutes} minutes"
        return "nothing scheduled in the next 8 hours"

    def _label_entities(self, label: str) -> list[str]:
        entities = cast("Any", self).label_entities(label)
        return [str(entity) for entity in entities] if isinstance(entities, list) else []

    def _state_value(self, entity_id: str) -> str | int | float:
        value = self.get_state(entity_id)
        if (
            isinstance(value, bool)
            or not isinstance(value, str | int | float)
            or value in INVALID_STATES
        ):
            return ""
        return value

    def _weather_summary(self, entity_id: str) -> str:
        condition = self._state_value(entity_id)
        temperature = self.get_state(entity_id, attribute="temperature")
        if not condition or temperature in INVALID_STATES:
            return ""
        return f"{condition}, {temperature}°C"

    @staticmethod
    def _fallback_message(config: HabitConfig) -> str:
        if config.habit_type is HabitType.BINARY:
            return f"Don't forget to mark {config.name} as complete!"
        return f"Don't forget to track {config.name}!"

    def _handle_notification_action(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        del event_type, kwargs
        action = data.get("action")
        if not isinstance(action, str):
            return
        parts = action.split("__")
        if len(parts) != ACTION_PARTS:
            return
        verb, user_upper, slot_raw = parts
        user = user_upper.lower()
        try:
            slot = int(slot_raw)
            config = self.store.data.users[user].habits[slot]
        except (KeyError, ValueError):
            return
        current = (
            self.store.data.users[user]
            .completions.get(slot, {})
            .get(
                self.datetime().date().isoformat(),
                0,
            )
        )
        if verb == "MARK_HABIT_AS_COMPLETE" and config.habit_type is HabitType.BINARY:
            self._set_completion(user, slot, 1)
        elif verb == "INCREMENT_HABIT" and config.habit_type is HabitType.COUNTABLE:
            self._set_completion(user, slot, current + 1)

    def _midnight_rollover(self, kwargs: dict[str, Any]) -> None:
        user = str(kwargs["user"])
        data = self.store.data.users[user]
        today = self.datetime().date()
        yesterday_key = (today - timedelta(days=1)).isoformat()
        if data.mood_today != "Not Set":
            data.mood_history[yesterday_key] = data.mood_today
        data.mood_today = "Not Set"
        data.mood_note = ""
        for slot in list(data.habits):
            self._clear_pending(user, slot)
            self.reminders.cancel_end_of_day(user, slot)
        data.end_of_day_reminder_sent_days.clear()
        self._clear_mood_pending(user)
        for config in data.habits.values():
            if config.configured:
                self._seed_next_reminder(user, config, force=True)
                self._schedule_end_of_day_reminder(user, config)
        self._seed_mood_reminder(user, force=True)
        # Progress records are keyed by day, so a stale one is replaced on the
        # next evaluation rather than cleared here.
        for slot, config in data.habits.items():
            if config.configured and config.completion_mode.is_event_driven:
                self.templates.cancel_duration(user, slot)
                self._schedule_evaluation(user, slot)
        self.store.save()
        self._publish_mood_state(user)
        for slot in data.habits:
            self._publish_habit_state(user, slot)
            self._publish_next_reminder(user, slot)

    def _normalize_user_slots(
        self,
        user: str,
    ) -> tuple[int | None, tuple[int, ...]]:
        """Keep the lowest stable spare and retire every other empty slot."""
        data = self.store.data.users[user]
        added_spare, retired = normalize_spare_slot(data)
        for slot in retired:
            with suppress(AttributeError):
                self.reminders.remove(user, slot)
            with suppress(AttributeError):
                self.templates.remove(user, slot)
            self._watched_entities.pop((user, slot), None)
            self._template_errors.pop((user, slot), None)
            self._published_template_attributes.pop((user, slot), None)
        return added_spare, retired

    def _validate_config(self) -> bool:  # noqa: C901, PLR0912
        errors: list[str] = []
        if len(set(self.users)) != len(self.users):
            errors.append("users (duplicates)")
        if (
            not isinstance(self.args.get("ai_task_entity"), str)
            or not self.args["ai_task_entity"]
        ):
            errors.append("ai_task_entity")
        if not isinstance(self.args.get("mqtt_host"), str) or not self.args["mqtt_host"]:
            errors.append("mqtt_host")
        try:
            port = int(self.args.get("mqtt_port", 1883))
            if not 1 <= port <= MAX_NETWORK_PORT:
                errors.append("mqtt_port")
        except (TypeError, ValueError):
            errors.append("mqtt_port")
        try:
            if int(self.args.get("mqtt_qos", 0)) not in {0, 1, 2}:
                errors.append("mqtt_qos")
        except (TypeError, ValueError):
            errors.append("mqtt_qos")
        try:
            debounce = float(self.args.get("template_eval_debounce_seconds", 2))
            if not 0 <= debounce <= MAX_DEBOUNCE_SECONDS:
                errors.append("template_eval_debounce_seconds")
        except (TypeError, ValueError):
            errors.append("template_eval_debounce_seconds")
        for key in ("mqtt_discovery_prefix", "mqtt_base_topic"):
            value = self.args.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip("/")):
                errors.append(key)
        configs = self.args.get("user_config")
        if not isinstance(configs, dict):
            errors.append("user_config")
        else:
            for user in self.users:
                config = configs.get(user)
                if not isinstance(config, dict):
                    errors.append(f"user_config.{user}")
                    continue
                errors.extend(
                    f"user_config.{user}.{key}"
                    for key in REQUIRED_USER_KEYS
                    if not isinstance(config.get(key), str) or not config[key]
                )
        labels = self.args.get("context_labels")
        if not isinstance(labels, dict):
            errors.append("context_labels")
        else:
            errors.extend(
                f"context_labels.{key}"
                for key in ("home", "work", "family")
                if not isinstance(labels.get(key), str) or not labels[key]
            )
            calendars = labels.get("calendar")
            if not isinstance(calendars, dict):
                errors.append("context_labels.calendar")
            else:
                errors.extend(
                    f"context_labels.calendar.{user}"
                    for user in self.users
                    if not isinstance(calendars.get(user), str) or not calendars[user]
                )
        if errors:
            self.error("Invalid habit tracker configuration: %s", ", ".join(errors))
            return False
        return True

    def _slot_attributes(self, config: HabitConfig) -> dict[str, object]:
        return {
            "configured": config.configured,
            "habit_name": config.name,
            "habit_type": config.habit_type,
            "slot": config.slot,
        }

    def _context_labels(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self.args["context_labels"])

    def _user_config(self, user: str) -> dict[str, Any]:
        configs = self.args.get("user_config", {})
        if not isinstance(configs, dict) or not isinstance(configs.get(user), dict):
            raise TypeError(f"missing user_config for {user}")
        return cast("dict[str, Any]", configs[user])


def _mqtt_bool(payload: str) -> bool:
    normalized = payload.strip().lower()
    if normalized in {"on", "true", "1"}:
        return True
    if normalized in {"off", "false", "0"}:
        return False
    raise ValueError("expected an on/off value")


def _local_now_label(value: datetime) -> str:
    """Compact day/month/time for AI context without a calendar date."""
    return (
        f"It's a {value.strftime('%A')} in {value.strftime('%B')} "
        f"and it's {value.strftime('%H:%M')}"
    )


def _service_result(response: object) -> dict[str, Any] | None:
    """Unwrap AppDaemon's call_service envelope to the Home Assistant result body."""
    if not isinstance(response, dict):
        return None
    if response.get("success") is False:
        return None
    result = response.get("result")
    if isinstance(result, dict):
        return cast("dict[str, Any]", result)
    return cast("dict[str, Any]", response)


def _ai_response_text(response: object) -> str | None:
    payload = _service_result(response)
    if payload is None:
        return None
    candidates: list[object] = [payload.get("data")]
    for key in ("response", "service_response", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested.get("data"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _bounded_int(payload: str, minimum: int, maximum: int) -> int:
    value = int(float(payload))
    if not minimum <= value <= maximum:
        raise ValueError(f"value must be between {minimum} and {maximum}")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_datetime(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if now.tzinfo is None:
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)
