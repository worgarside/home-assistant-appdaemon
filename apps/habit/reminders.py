"""Reminder scheduling from absolute next-fire datetimes."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Protocol


class Scheduler(Protocol):
    """AppDaemon timer methods used by the reminder manager."""

    def run_in(self, callback: Any, delay: float, **kwargs: Any) -> str:
        """Register a delayed callback."""
        ...

    def cancel_timer(self, handle: str) -> bool:
        """Cancel a timer."""
        ...


class ReminderManager:
    """Manage one cancellable absolute-time timer per habit slot."""

    def __init__(self, scheduler: Scheduler, callback: Any) -> None:
        self._scheduler = scheduler
        self._callback = callback
        self._timers: dict[tuple[str, int], str] = {}

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
        self.cancel(user, slot)
        delay = max(0, (fire_at - now).total_seconds())
        self._timers[(user, slot)] = self._scheduler.run_in(
            self._callback,
            delay,
            user=user,
            slot=slot,
            reminder_index=reminder_index,
            final_index=final_index,
        )

    def cancel(self, user: str, slot: int) -> None:
        """Cancel the pending timer for a slot."""
        self._cancel_handle(self._timers.pop((user, slot), None))

    def remove(self, user: str, slot: int) -> None:
        """Remove schedules for a retired slot."""
        self.cancel(user, slot)

    def cancel_all(self) -> None:
        """Cancel all managed timers."""
        for handle in self._timers.values():
            self._cancel_handle(handle)
        self._timers.clear()

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
