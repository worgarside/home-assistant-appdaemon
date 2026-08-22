"""Regression tests for opt-in end-of-day habit reminders."""

from __future__ import annotations

# unittest keeps these dependency-free; the project does not depend on pytest.
# ruff: noqa: PT009
from datetime import UTC, datetime
from typing import Any
from unittest import TestCase

from apps.habit.models import SCHEMA_VERSION, HabitConfig, StoreData, UserData
from apps.habit.reminders import ReminderManager, end_of_day_reminder_fire_at


class FakeScheduler:
    """Record timer operations without starting AppDaemon."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[float, dict[str, Any]]] = []
        self.cancelled: list[str] = []

    def run_in(self, callback: Any, delay: float, **kwargs: Any) -> str:
        """Record a scheduled callback and return a stable fake handle."""
        del callback
        self.scheduled.append((delay, kwargs))
        return f"timer-{len(self.scheduled)}"

    def cancel_timer(self, handle: str, *, silent: bool = False) -> bool:
        """Record a cancelled handle."""
        del silent
        self.cancelled.append(handle)
        return True


class EndOfDayReminderTests(TestCase):
    """Verify migration, persistence, and independent timer behavior."""

    def test_schema_v2_defaults_end_of_day_reminder_to_disabled(self) -> None:
        """Existing stores must migrate without enabling surprise notifications."""
        store = StoreData.from_dict(
            {
                "schema_version": 2,
                "users": {
                    "will": {
                        "habits": {"1": {"slot": 1, "name": "Read"}},
                        "completions": {},
                        "pending_reminders": {},
                        "template_progress": {},
                        "mood_history": {},
                    },
                },
            },
        )

        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertFalse(
            store.users["will"].habits[1].end_of_day_reminder_enabled,
        )

    def test_end_of_day_setting_round_trips(self) -> None:
        """The per-habit opt-in must survive persistence."""
        config = HabitConfig(
            slot=1,
            name="Read",
            end_of_day_reminder_enabled=True,
        )

        restored = HabitConfig.from_dict(config.to_dict())

        self.assertTrue(restored.end_of_day_reminder_enabled)

    def test_sent_day_round_trips(self) -> None:
        """The at-most-once marker must survive an AppDaemon restart."""
        data = UserData(end_of_day_reminder_sent_days={1: "2026-08-22"})

        restored = UserData.from_dict(data.to_dict())

        self.assertEqual(
            restored.end_of_day_reminder_sent_days,
            {1: "2026-08-22"},
        )

    def test_end_of_day_reminder_does_not_rearm_after_firing(self) -> None:
        """Restoring or editing a habit after 23:55 must not duplicate it."""
        now = datetime(2026, 8, 22, 23, 57, tzinfo=UTC)

        fire_at = end_of_day_reminder_fire_at(
            now,
            last_sent_day="2026-08-22",
        )

        self.assertIsNone(fire_at)

    def test_late_start_arms_unsent_end_of_day_reminder_immediately(self) -> None:
        """An unsent final check should still fire after the nominal time."""
        now = datetime(2026, 8, 22, 23, 57, tzinfo=UTC)

        fire_at = end_of_day_reminder_fire_at(now, last_sent_day=None)

        self.assertEqual(fire_at, now)

    def test_end_of_day_timer_is_independent_from_regular_timer(self) -> None:
        """Cancelling the final check must not alter the repeat chain."""
        scheduler = FakeScheduler()

        def callback(_kwargs: dict[str, Any]) -> None:
            """Satisfy the reminder callback contract."""

        manager = ReminderManager(scheduler, callback)
        now = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

        manager.schedule_at(
            "will",
            1,
            fire_at=datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
            reminder_index=1,
            final_index=3,
            now=now,
        )
        manager.schedule_end_of_day(
            "will",
            1,
            fire_at=datetime(2026, 8, 22, 23, 55, tzinfo=UTC),
            now=now,
        )
        manager.cancel_end_of_day("will", 1)
        manager.cancel("will", 1)

        self.assertEqual(
            scheduler.scheduled,
            [
                (
                    3600.0,
                    {
                        "user": "will",
                        "reminder_index": 1,
                        "final_index": 3,
                        "kind": "habit",
                        "slot": 1,
                    },
                ),
                (
                    53700.0,
                    {
                        "user": "will",
                        "reminder_index": 1,
                        "final_index": 1,
                        "kind": "end_of_day",
                        "slot": 1,
                    },
                ),
            ],
        )
        self.assertEqual(scheduler.cancelled, ["timer-2", "timer-1"])
