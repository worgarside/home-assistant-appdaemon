"""Validated domain models and streak calculations for habit tracking."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Final, Self

SCHEMA_VERSION: Final[int] = 1
MAX_NAME_LENGTH: Final[int] = 255
MAX_REPEAT_COUNT: Final[int] = 100
MAX_REPEAT_INTERVAL: Final[int] = 1440
MAX_DAYS_PER_WEEK: Final[int] = 7
MAX_STREAK_DAYS: Final[int] = 366
MOOD_OPTIONS: Final[tuple[str, ...]] = (
    "Not Set",
    "Very Low",
    "Low",
    "Okay",
    "Good",
    "Great",
)


class HabitType(StrEnum):
    """Supported habit completion models."""

    BINARY = "binary"
    COUNTABLE = "countable"


@dataclass(slots=True)
class HabitConfig:
    """Persistent configuration for one stable numbered habit slot."""

    slot: int
    name: str = ""
    habit_type: HabitType = HabitType.BINARY
    reminder_time: str = "09:00:00"
    repeat_count: int = 0
    repeat_interval_minutes: int = 60
    streak_min_days_per_week: int = 7
    ai_enabled: bool = False
    icon_on: str = "mdi:check-circle"
    icon_active: str = "mdi:counter"
    icon_off: str = "mdi:circle-outline"
    icon_zero: str = "mdi:counter"

    def __post_init__(self) -> None:
        """Reject malformed persisted or MQTT-provided configuration."""
        if self.slot < 1:
            raise ValueError("slot must be positive")
        if len(self.name) > MAX_NAME_LENGTH:
            raise ValueError("habit name is too long")
        if not 0 <= self.repeat_count <= MAX_REPEAT_COUNT:
            raise ValueError("repeat_count must be between 0 and 100")
        if not 1 <= self.repeat_interval_minutes <= MAX_REPEAT_INTERVAL:
            raise ValueError("repeat_interval_minutes must be between 1 and 1440")
        if not 1 <= self.streak_min_days_per_week <= MAX_DAYS_PER_WEEK:
            raise ValueError("streak_min_days_per_week must be between 1 and 7")
        try:
            time.fromisoformat(self.reminder_time)
        except ValueError as error:
            raise ValueError("reminder_time must use HH:MM:SS") from error

    @property
    def configured(self) -> bool:
        """Return whether the slot has a user-facing name."""
        return bool(self.name.strip())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the configuration to JSON-compatible values."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        """Parse and validate a configuration mapping."""
        return cls(
            slot=_integer(value, "slot"),
            name=_string(value, "name", ""),
            habit_type=HabitType(_string(value, "habit_type", HabitType.BINARY)),
            reminder_time=_string(value, "reminder_time", "09:00:00"),
            repeat_count=_integer(value, "repeat_count", 0),
            repeat_interval_minutes=_integer(
                value,
                "repeat_interval_minutes",
                60,
            ),
            streak_min_days_per_week=_integer(
                value,
                "streak_min_days_per_week",
                7,
            ),
            ai_enabled=_boolean(value, "ai_enabled", default=False),
            icon_on=_string(value, "icon_on", "mdi:check-circle"),
            icon_active=_string(value, "icon_active", "mdi:counter"),
            icon_off=_string(value, "icon_off", "mdi:circle-outline"),
            icon_zero=_string(value, "icon_zero", "mdi:counter"),
        )


@dataclass(slots=True)
class PendingReminder:
    """Durable next-fire metadata for one habit slot."""

    fire_at: str
    next_index: int
    final_index: int

    def __post_init__(self) -> None:
        """Reject malformed reminder state."""
        datetime.fromisoformat(self.fire_at)
        if self.next_index < 1:
            raise ValueError("next_index must be positive")
        if self.final_index < 1:
            raise ValueError("final_index must be positive")
        if self.next_index > self.final_index:
            raise ValueError("next_index cannot exceed final_index")

    def to_dict(self) -> dict[str, Any]:
        """Serialize pending reminder state."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        """Parse and validate pending reminder state."""
        pending = cls(
            fire_at=_string(value, "fire_at"),
            next_index=_integer(value, "next_index"),
            final_index=_integer(value, "final_index"),
        )
        pending.__post_init__()
        return pending


@dataclass(slots=True)
class UserData:
    """All disk-backed state for one configured user."""

    habits: dict[int, HabitConfig] = field(default_factory=dict)
    completions: dict[int, dict[str, int]] = field(default_factory=dict)
    pending_reminders: dict[int, PendingReminder] = field(default_factory=dict)
    mood_history: dict[str, str] = field(default_factory=dict)
    mood_today: str = "Not Set"
    mood_note: str = ""
    mood_reminder_time: str = "20:00:00"
    mood_reminders_enabled: bool = True
    mood_repeat_count: int = 0
    mood_repeat_interval_minutes: int = 60
    pending_mood_reminder: PendingReminder | None = None

    def __post_init__(self) -> None:
        """Reject malformed mood reminder configuration."""
        if not 0 <= self.mood_repeat_count <= MAX_REPEAT_COUNT:
            raise ValueError("mood_repeat_count must be between 0 and 100")
        if not 1 <= self.mood_repeat_interval_minutes <= MAX_REPEAT_INTERVAL:
            raise ValueError(
                "mood_repeat_interval_minutes must be between 1 and 1440",
            )
        try:
            time.fromisoformat(self.mood_reminder_time)
        except ValueError as error:
            raise ValueError("mood_reminder_time must use HH:MM:SS") from error

    def to_dict(self) -> dict[str, Any]:
        """Serialize user data."""
        return {
            "habits": {
                str(slot): config.to_dict() for slot, config in self.habits.items()
            },
            "completions": {
                str(slot): values for slot, values in self.completions.items()
            },
            "pending_reminders": {
                str(slot): pending.to_dict()
                for slot, pending in self.pending_reminders.items()
            },
            "mood_history": self.mood_history,
            "mood_today": self.mood_today,
            "mood_note": self.mood_note,
            "mood_reminder_time": self.mood_reminder_time,
            "mood_reminders_enabled": self.mood_reminders_enabled,
            "mood_repeat_count": self.mood_repeat_count,
            "mood_repeat_interval_minutes": self.mood_repeat_interval_minutes,
            "pending_mood_reminder": (
                None
                if self.pending_mood_reminder is None
                else self.pending_mood_reminder.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        """Parse and validate persisted user data."""
        raw_habits = _mapping(value, "habits")
        raw_completions = _mapping(value, "completions")
        habits = {
            int(slot): HabitConfig.from_dict(_dict(item))
            for slot, item in raw_habits.items()
        }
        completions: dict[int, dict[str, int]] = {}
        for slot, entries in raw_completions.items():
            completions[int(slot)] = {
                _date_string(day): _nonnegative_integer(count)
                for day, count in _dict(entries).items()
            }
        pending_reminders = {
            int(slot): PendingReminder.from_dict(_dict(item))
            for slot, item in _mapping(value, "pending_reminders").items()
        }
        mood_history = {
            _date_string(day): _mood(mood)
            for day, mood in _mapping(value, "mood_history").items()
        }
        raw_pending_mood = value.get("pending_mood_reminder")
        pending_mood = (
            None
            if raw_pending_mood in (None, {})
            else PendingReminder.from_dict(_dict(raw_pending_mood))
        )
        return cls(
            habits=habits,
            completions=completions,
            pending_reminders=pending_reminders,
            mood_history=mood_history,
            mood_today=_mood(value.get("mood_today", "Not Set")),
            mood_note=_string(value, "mood_note", ""),
            mood_reminder_time=_string(value, "mood_reminder_time", "20:00:00"),
            mood_reminders_enabled=_boolean(
                value,
                "mood_reminders_enabled",
                default=True,
            ),
            mood_repeat_count=_integer(value, "mood_repeat_count", 0),
            mood_repeat_interval_minutes=_integer(
                value,
                "mood_repeat_interval_minutes",
                60,
            ),
            pending_mood_reminder=pending_mood,
        )


@dataclass(slots=True)
class StoreData:
    """Top-level versioned persistence schema."""

    users: dict[str, UserData]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serialize the complete store."""
        return {
            "schema_version": self.schema_version,
            "users": {user: data.to_dict() for user, data in self.users.items()},
        }

    @classmethod
    def empty(cls, users: tuple[str, ...]) -> Self:
        """Create an empty store with one spare slot per user."""
        return cls(
            users={user: UserData(habits={1: HabitConfig(slot=1)}) for user in users},
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        """Parse the current schema, rejecting unsupported versions."""
        version = _integer(value, "schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {version}")
        users = {
            str(user): UserData.from_dict(_dict(data))
            for user, data in _mapping(value, "users").items()
        }
        return cls(
            users=users,
            schema_version=version,
        )


@dataclass(frozen=True, slots=True)
class StreakStats:
    """Derived habit history metrics."""

    streak: int
    days_since_completion: int
    completion_rate_28_days: float


def calculate_streak(
    completions: dict[str, int],
    *,
    today: date,
    min_days_per_week: int,
) -> StreakStats:
    """Calculate strict daily or weekly-qualified calendar-day streaks."""
    completed = {
        date.fromisoformat(day) for day, count in completions.items() if count > 0
    }
    anchor = today if today in completed else today - timedelta(days=1)
    streak = (
        _strict_daily_streak(completed, anchor)
        if min_days_per_week == MAX_DAYS_PER_WEEK
        else _weekly_streak(
            completed,
            anchor=anchor,
            today=today,
            min_days_per_week=min_days_per_week,
        )
    )
    latest = max(completed, default=None)
    days_since = -1 if latest is None else (today - latest).days
    completed_28 = sum(
        today - timedelta(days=27) <= completed_day <= today
        for completed_day in completed
    )
    return StreakStats(streak, days_since, round(completed_28 / 28 * 100, 1))


def calculate_mood_streak(mood_history: dict[str, str], *, today: date) -> int:
    """Count consecutive completed mood entries ending yesterday."""
    streak = 0
    checked = today - timedelta(days=1)
    while mood_history.get(checked.isoformat()) in MOOD_OPTIONS[1:]:
        streak += 1
        checked -= timedelta(days=1)
    return streak


def normalize_spare_slot(data: UserData) -> tuple[int | None, tuple[int, ...]]:
    """Keep exactly the lowest stable unconfigured slot.

    Retired slots must lose their completion history. Otherwise a later habit can
    reuse the same numeric slot and incorrectly inherit the deleted habit's streak.
    """
    empty_slots = sorted(
        slot for slot, habit in data.habits.items() if not habit.configured
    )
    added_spare: int | None = None
    if empty_slots:
        spare_slot = empty_slots[0]
    else:
        spare_slot = 1
        while spare_slot in data.habits:
            spare_slot += 1
        data.habits[spare_slot] = HabitConfig(slot=spare_slot)
        added_spare = spare_slot
    retired = tuple(slot for slot in empty_slots if slot != spare_slot)
    for slot in retired:
        del data.habits[slot]
        data.completions.pop(slot, None)
        data.pending_reminders.pop(slot, None)
    return added_spare, retired


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _strict_daily_streak(completed: set[date], anchor: date) -> int:
    if anchor not in completed:
        return 0
    streak = 0
    checked = anchor
    while checked in completed and streak < MAX_STREAK_DAYS:
        streak += 1
        checked -= timedelta(days=1)
    return streak


def _weekly_streak(
    completed: set[date],
    *,
    anchor: date,
    today: date,
    min_days_per_week: int,
) -> int:
    if anchor not in completed or (today - anchor).days > 1:
        return 0
    anchor_week = _week_start(anchor)
    streak = (anchor - anchor_week).days + 1
    checked_week = anchor_week - timedelta(days=7)
    for _ in range(52):
        completed_days = sum(
            checked_week <= completed_day <= checked_week + timedelta(days=6)
            for completed_day in completed
        )
        if completed_days < min_days_per_week:
            break
        streak += 7
        checked_week -= timedelta(days=7)
    return streak


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return {str(key): item for key, item in value.items()}


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    return _dict(value.get(key, {}))


def _string(
    value: dict[str, Any],
    key: str,
    default: str | HabitType | None = None,
) -> str:
    item = value.get(key, default)
    if not isinstance(item, str):
        raise TypeError(f"{key} must be a string")
    return item


def _integer(value: dict[str, Any], key: str, default: int | None = None) -> int:
    item = value.get(key, default)
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError(f"{key} must be an integer")
    return item


def _nonnegative_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("completion count must be a non-negative integer")
    return value


def _boolean(value: dict[str, Any], key: str, *, default: bool) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise TypeError(f"{key} must be a boolean")
    return item


def _date_string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("date key must be a string")
    date.fromisoformat(value)
    return value


def _mood(value: object) -> str:
    if not isinstance(value, str) or value not in MOOD_OPTIONS:
        raise ValueError("invalid mood")
    return value
