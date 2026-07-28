"""Template dependency extraction, truthiness coercion, and listener bookkeeping."""

from __future__ import annotations

import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

# Matches a bare `domain.object_id` pair. The lookaround refuses to match
# inside a longer dotted chain such as `states.sensor.foo`, which has no single
# entity dependency to subscribe to.
ENTITY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.])([a-z_][a-z0-9_]*)\.([a-z0-9_]+)(?![\w.])",
)

TRUTHY_STRINGS: Final[frozenset[str]] = frozenset({"true", "on", "yes", "1"})
FALSY_STRINGS: Final[frozenset[str]] = frozenset(
    {
        "",
        "0",
        "false",
        "no",
        "none",
        "null",
        "off",
        "unavailable",
        "unknown",
    },
)


def extract_candidate_entities(template: str) -> tuple[str, ...]:
    """Return unique `domain.object_id` candidates in first-seen order.

    These are candidates only: the pattern cannot tell an entity reference from
    an attribute access such as `value.split`. Callers filter against real
    Home Assistant state before subscribing.
    """
    seen: dict[str, None] = {}
    for match in ENTITY_PATTERN.finditer(template):
        seen.setdefault(f"{match.group(1)}.{match.group(2)}", None)
    return tuple(seen)


def coerce_truthy(value: object) -> bool | None:
    """Coerce a rendered template result to a boolean.

    Returns ``None`` when the value is not recognisable, so the caller can log
    it once and fall back to False. Home Assistant returns a native bool for a
    single pure expression but a string for anything else, and ``bool("False")``
    is ``True``, so results must never be tested directly.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value > 0
    if not isinstance(value, str):
        return False if value is None else None
    normalized = value.strip().lower()
    if normalized in TRUTHY_STRINGS | FALSY_STRINGS:
        return normalized in TRUTHY_STRINGS
    try:
        return float(normalized) > 0
    except ValueError:
        return None


class TemplateScheduler(Protocol):
    """AppDaemon methods used by the template watcher."""

    def run_in(self, callback: Any, delay: float, **kwargs: Any) -> str:
        """Register a delayed callback."""
        ...

    def cancel_timer(self, handle: str, *, silent: bool = False) -> bool:
        """Cancel a timer."""
        ...

    def listen_state(self, callback: Any, entity_id: Any, **kwargs: Any) -> Any:
        """Subscribe to state changes for an entity."""
        ...

    def cancel_listen_state(self, handle: Any) -> Any:
        """Cancel a state subscription."""
        ...


class TemplateWatcher:
    """Own per-slot state subscriptions, evaluation timers, and duration timers."""

    def __init__(
        self,
        scheduler: TemplateScheduler,
        on_change: Any,
        on_evaluate: Any,
        on_duration: Any,
    ) -> None:
        self._scheduler = scheduler
        self._on_change = on_change
        self._on_evaluate = on_evaluate
        self._on_duration = on_duration
        self._listeners: dict[tuple[str, int], tuple[Any, ...]] = {}
        self._pending: dict[tuple[str, int], str] = {}
        self._durations: dict[tuple[str, int], str] = {}

    def watch(self, user: str, slot: int, entities: Iterable[str]) -> tuple[str, ...]:
        """Replace the subscriptions for a slot and return what is watched."""
        self.unwatch(user, slot)
        watched = tuple(dict.fromkeys(entities))
        handles = [
            self._scheduler.listen_state(self._on_change, entity, user=user, slot=slot)
            for entity in watched
        ]
        if handles:
            self._listeners[(user, slot)] = tuple(handles)
        return watched

    def unwatch(self, user: str, slot: int) -> None:
        """Drop every subscription for a slot."""
        for handle in self._listeners.pop((user, slot), ()):
            with suppress(Exception):
                self._scheduler.cancel_listen_state(handle)

    def schedule(self, user: str, slot: int, delay: float) -> None:
        """Debounce an evaluation, replacing any already-pending one."""
        self.cancel_scheduled(user, slot)
        self._pending[(user, slot)] = self._scheduler.run_in(
            self._on_evaluate,
            delay,
            user=user,
            slot=slot,
        )

    def cancel_scheduled(self, user: str, slot: int) -> None:
        """Cancel a pending evaluation for a slot."""
        handle = self._pending.pop((user, slot), None)
        if handle is not None:
            self._scheduler.cancel_timer(handle, silent=True)

    def release(self, user: str, slot: int) -> None:
        """Drop an evaluation handle that has already fired."""
        self._pending.pop((user, slot), None)

    def arm_duration(self, user: str, slot: int, delay: float) -> None:
        """Replace the duration timer for a slot."""
        self.cancel_duration(user, slot)
        self._durations[(user, slot)] = self._scheduler.run_in(
            self._on_duration,
            delay,
            user=user,
            slot=slot,
        )

    def cancel_duration(self, user: str, slot: int) -> None:
        """Cancel the duration timer for a slot."""
        handle = self._durations.pop((user, slot), None)
        if handle is not None:
            self._scheduler.cancel_timer(handle, silent=True)

    def release_duration(self, user: str, slot: int) -> None:
        """Drop a duration handle that has already fired."""
        self._durations.pop((user, slot), None)

    def remove(self, user: str, slot: int) -> None:
        """Forget a retired slot entirely."""
        self.cancel_scheduled(user, slot)
        self.cancel_duration(user, slot)
        self.unwatch(user, slot)

    def cancel_all(self) -> None:
        """Cancel every subscription and pending timer."""
        for user, slot in list(self._pending):
            self.cancel_scheduled(user, slot)
        for user, slot in list(self._durations):
            self.cancel_duration(user, slot)
        for user, slot in list(self._listeners):
            self.unwatch(user, slot)
