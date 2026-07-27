"""Reminder scheduling with cancellable repeat chains."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Protocol


class Scheduler(Protocol):
    """AppDaemon timer methods used by the reminder manager."""

    def run_daily(
        self,
        callback: Any,
        start: time,
        **kwargs: Any,
    ) -> str:
        """Register a daily callback."""
        ...

    def run_in(self, callback: Any, delay: int, **kwargs: Any) -> str:
        """Register a delayed callback."""
        ...

    def cancel_timer(self, handle: str) -> bool:
        """Cancel a timer."""
        ...


class ReminderManager:
    """Manage daily schedules and per-habit repeat timers."""

    def __init__(self, scheduler: Scheduler, callback: Any) -> None:
        self._scheduler = scheduler
        self._callback = callback
        self._daily: dict[tuple[str, int], str] = {}
        self._repeats: dict[tuple[str, int], str] = {}

    def schedule_daily(self, user: str, slot: int, reminder_time: str) -> None:
        """Replace the daily schedule for a habit."""
        key = (user, slot)
        self._cancel_handle(self._daily.pop(key, None))
        parsed = time.fromisoformat(reminder_time)
        self._daily[key] = self._scheduler.run_daily(
            self._callback,
            parsed,
            user=user,
            slot=slot,
            reminder_index=1,
        )

    def schedule_next_repeat(
        self,
        user: str,
        slot: int,
        *,
        next_index: int,
        final_index: int,
        interval_minutes: int,
        now: datetime,
    ) -> bool:
        """Schedule only the next repeat when it fits before the cutoff."""
        self.cancel_repeats(user, slot)
        if next_index > final_index or not repeat_fits_before_midnight(
            now,
            interval_minutes,
        ):
            return False
        key = (user, slot)
        self._repeats[key] = self._scheduler.run_in(
            self._callback,
            interval_minutes * 60,
            user=user,
            slot=slot,
            reminder_index=next_index,
            final_index=final_index,
        )
        return True

    def cancel_repeats(self, user: str, slot: int) -> None:
        """Cancel pending repeats after completion."""
        self._cancel_handle(self._repeats.pop((user, slot), None))

    def remove(self, user: str, slot: int) -> None:
        """Remove all schedules for a retired slot."""
        self.cancel_repeats(user, slot)
        self._cancel_handle(self._daily.pop((user, slot), None))

    def cancel_all(self) -> None:
        """Cancel all managed timers."""
        for handle in self._daily.values():
            self._cancel_handle(handle)
        for handle in self._repeats.values():
            self._cancel_handle(handle)
        self._daily.clear()
        self._repeats.clear()

    def _cancel_handle(self, handle: str | None) -> None:
        if handle is not None:
            self._scheduler.cancel_timer(handle)


def repeat_fits_before_midnight(now: datetime, interval_minutes: int) -> bool:
    """Apply the legacy cutoff: midnight minus interval plus five minutes."""
    next_midnight = datetime.combine(
        now.date() + timedelta(days=1),
        time(),
        tzinfo=now.tzinfo,
    )
    cutoff = next_midnight - timedelta(minutes=interval_minutes + 5)
    return now < cutoff
