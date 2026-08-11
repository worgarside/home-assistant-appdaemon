"""Reminder scheduling from absolute next-fire datetimes."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Protocol


class Scheduler(Protocol):
    """AppDaemon timer methods used by the reminder manager."""

    def run_in(self, callback: Any, delay: float, **kwargs: Any) -> str:
        """Register a delayed callback."""
        ...

    def cancel_timer(self, handle: str, *, silent: bool = False) -> bool:
        """Cancel a timer."""
        ...


class ReminderManager:
    """Manage one cancellable absolute-time timer per habit slot or mood."""

    def __init__(self, scheduler: Scheduler, callback: Any) -> None:
        self._scheduler = scheduler
        self._callback = callback
        self._timers: dict[tuple[str, str], str] = {}

    def schedule_at(
        self,
        user: str,
        slot: int,
        *,
        fire_at: datetime,
        reminder_index: int,
        final_index: int,
        now: datetime,
    ) -> None:
        """Replace the pending timer for a habit with an absolute fire time."""
        self._schedule(
            user,
            self._habit_key(slot),
            fire_at=fire_at,
            reminder_index=reminder_index,
            final_index=final_index,
            now=now,
            kind="habit",
            slot=slot,
        )

    def schedule_mood(
        self,
        user: str,
        *,
        fire_at: datetime,
        reminder_index: int,
        final_index: int,
        now: datetime,
    ) -> None:
        """Replace the pending mood timer with an absolute fire time."""
        self._schedule(
            user,
            "mood",
            fire_at=fire_at,
            reminder_index=reminder_index,
            final_index=final_index,
            now=now,
            kind="mood",
        )

    def cancel(self, user: str, slot: int) -> None:
        """Cancel the pending timer for a habit slot."""
        self._cancel_handle(self._timers.pop((user, self._habit_key(slot)), None))

    def cancel_mood(self, user: str) -> None:
        """Cancel the pending mood timer for a user."""
        self._cancel_handle(self._timers.pop((user, "mood"), None))

    def release(self, user: str, slot: int) -> None:
        """Drop a habit timer handle after it has already fired."""
        self._timers.pop((user, self._habit_key(slot)), None)

    def release_mood(self, user: str) -> None:
        """Drop a mood timer handle after it has already fired."""
        self._timers.pop((user, "mood"), None)

    def remove(self, user: str, slot: int) -> None:
        """Remove schedules for a retired slot."""
        self.cancel(user, slot)

    def cancel_all(self) -> None:
        """Cancel all managed timers."""
        for handle in self._timers.values():
            self._cancel_handle(handle)
        self._timers.clear()

    def _schedule(
        self,
        user: str,
        timer_key: str,
        *,
        fire_at: datetime,
        reminder_index: int,
        final_index: int,
        now: datetime,
        **callback_kwargs: Any,
    ) -> None:
        self._cancel_handle(self._timers.pop((user, timer_key), None))
        delay = max(0, (fire_at - now).total_seconds())
        self._timers[(user, timer_key)] = self._scheduler.run_in(
            self._callback,
            delay,
            user=user,
            reminder_index=reminder_index,
            final_index=final_index,
            **callback_kwargs,
        )

    def _cancel_handle(self, handle: str | None) -> None:
        if handle is not None:
            self._scheduler.cancel_timer(handle, silent=True)

    @staticmethod
    def _habit_key(slot: int) -> str:
        return f"habit:{slot}"


def repeat_fits_before_midnight(now: datetime, interval_minutes: int) -> bool:
    """Apply the legacy cutoff: midnight minus interval plus five minutes."""
    next_midnight = datetime.combine(
        now.date() + timedelta(days=1),
        time(),
        tzinfo=now.tzinfo,
    )
    cutoff = next_midnight - timedelta(minutes=interval_minutes + 5)
    return now < cutoff


def repeat_fits_before_logical_day_end(
    now: datetime,
    interval_minutes: int,
    *,
    boundary_hour: int = 4,
) -> bool:
    """Return whether a repeat fits before the next logical-day boundary."""
    boundary = datetime.combine(now.date(), time(boundary_hour), tzinfo=now.tzinfo)
    if now >= boundary:
        boundary += timedelta(days=1)
    cutoff = boundary - timedelta(minutes=interval_minutes + 5)
    return now < cutoff
