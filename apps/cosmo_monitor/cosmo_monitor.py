"""Monitors the Cosmo vacuum's cleaning history."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from json import dumps
from typing import Any, ClassVar, Final, Generic, Literal, Self, TypedDict, TypeVar

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from pydantic import BaseModel, ConfigDict, computed_field

UNAVAILABLE: Final[str] = "unavailable"


class StateValue(StrEnum):
    """A state value."""


class CosmoState(StateValue):
    """A Cosmo vacuum state."""

    CLEANING = "cleaning"
    DOCKED = "docked"
    IDLE = "idle"
    PAUSED = "paused"
    RETURNING_TO_DOCK = "returning"

    UNAVAILABLE = UNAVAILABLE


class TaskStatus(StateValue):
    """A Cosmo vacuum task status."""

    CLEANING = "cleaning"
    CLEANING_PAUSED = "cleaning_paused"
    COMPLETED = "completed"
    DOCKING_PAUSED = "docking_paused"
    FAST_MAPPING = "fast_mapping"
    MAP_CLEANING_PAUSED = "map_cleaning_paused"
    ROOM_CLEANING = "room_cleaning"
    ROOM_CLEANING_PAUSED = "room_cleaning_paused"
    SPOT_CLEANING = "spot_cleaning"
    ZONE_CLEANING = "zone_cleaning"

    UNAVAILABLE = UNAVAILABLE

    def is_room_cleaning(self) -> bool:
        """Return whether the task status is a room cleaning status."""
        return self in (
            TaskStatus.CLEANING,
            TaskStatus.ROOM_CLEANING,
            TaskStatus.ZONE_CLEANING,
        )

    def is_paused(self) -> bool:
        """Return whether the task status is a paused status."""
        return self in (
            TaskStatus.CLEANING_PAUSED,
            TaskStatus.DOCKING_PAUSED,
            TaskStatus.MAP_CLEANING_PAUSED,
            TaskStatus.ROOM_CLEANING_PAUSED,
        )


class Room(StateValue):
    """A room that Cosmo can vacuum."""

    BEDROOM = "Bedroom"
    EN_SUITE = "En-Suite"
    HALLWAY = "Hallway"
    KITCHEN = "Kitchen"
    LOUNGE = "Lounge"
    OFFICE = "Office"

    UNAVAILABLE = UNAVAILABLE


S = TypeVar("S", bound=StateValue)


class BaseStateTypeInfo(TypedDict, Generic[S]):
    """Information about a state type."""

    start_time: datetime
    end_time: datetime
    state: S


class StateTypeInfo(BaseStateTypeInfo[S]):
    """Information about a state type."""

    entity_id: str
    duration: timedelta
    last_changed: str
    last_updated: str


class State(BaseModel, Generic[S]):
    """A state entity."""

    start_time: datetime
    end_time: datetime
    state: S  # StrEnum

    model_config: ClassVar[ConfigDict] = {"extra": "ignore"}

    @computed_field  # type: ignore[misc]
    @property
    def duration(self) -> timedelta:
        """Return the duration of the state."""
        return self.end_time - self.start_time

    def __str__(self) -> str:
        """Return a string representation of the state."""
        return f"{self.state}\t{self.start_time!s} to {self.end_time!s}"


class _History(BaseModel, Generic[S]):
    """A history of a state entity."""

    entity_id: str
    states: list[State[S]]
    lower_limit: datetime
    upper_limit: datetime

    @computed_field  # type: ignore[misc]
    @property
    def duration(self) -> timedelta:
        """Return the total duration of the history."""
        return self.upper_limit - self.lower_limit

    @classmethod
    def from_state_history(
        cls: type[Self],
        entity_id: str,
        /,
        hass: Hass,
        *,
        lower_limit: datetime,
        upper_limit: datetime | None = None,
        remove_unavailable_states: bool = True,
        reverse: bool = False,
    ) -> Self:
        """Create a history from the Home Assistant history API.

        If `upper_limit` is None, then the current time is used.

        Args:
            entity_id: The entity ID to get the history of.
            hass: The Home Assistant instance.
            lower_limit: The lower limit of the history.
            upper_limit: The upper limit of the history. If None, the current time is used.
            remove_unavailable_states: Whether to remove unavailable states from the history.
            reverse: Whether to reverse the history.

        Returns:
            History of the entity.
        """
        lower_limit = lower_limit.astimezone(UTC).replace(tzinfo=None)

        end_time = (
            upper_limit.astimezone(UTC).replace(tzinfo=None)
            if upper_limit
            else datetime.utcnow()
        )

        hass_history = hass.get_history(
            entity_id=entity_id,
            start_time=lower_limit.astimezone(UTC).replace(tzinfo=None),
            end_time=end_time,
        )

        if not hass_history or len(hass_history) != 1:
            raise ValueError(  # noqa: TRY003
                "Unexpected response from Home Assistant"
                f" history API: {hass_history!r}",
            )

        state_history: list[StateTypeInfo[S]] = sorted(  # Oldest -> newest
            hass_history[0],
            key=lambda x: x["last_updated"],
        )

        del hass_history

        hass.log(
            "Found %i states for %s between %s and %s",
            len(state_history),
            entity_id,
            lower_limit,
            upper_limit or "now",
        )

        hass.log("First five items: %s", dumps(state_history[:5], default=str))

        # Descending order (newest -> oldest)
        merged_states: list[BaseStateTypeInfo[S]] = []
        while state_history:
            state = state_history.pop()  # Most recent state

            if (
                last_changed := datetime.fromisoformat(state["last_changed"])
                .astimezone(UTC)
                .replace(tzinfo=None)
            ) > end_time or (
                remove_unavailable_states and state["state"] == UNAVAILABLE
            ):
                # Ignore this state because it's after the upper limit (or unavailable and unavailable
                # states should be removed)
                continue

            if lower_limit_broken := last_changed < lower_limit:
                # Bring last changed forward to lower limit if it's before it
                state["start_time"] = lower_limit
            else:
                state["start_time"] = last_changed

            if not merged_states:
                # If this is the first iteration/last state
                state["end_time"] = end_time
            elif (next_state := merged_states[-1])["state"] == state["state"]:
                next_state["start_time"] = state["start_time"]
                continue
            else:
                # Otherwise, set the end time to the start time of the previous iteration/next state
                state["end_time"] = next_state["start_time"]

            merged_states.append(
                {
                    "start_time": state["start_time"],
                    "end_time": state["end_time"],
                    "state": state["state"],
                },
            )

            # If this state broke the lower limit (for the first time), then it has been rounded up and the
            # loop should be broken
            if lower_limit_broken:
                break

        # merged_states is already in reverse order, so only flip if reverse is False
        return cls.model_validate(
            {
                "entity_id": entity_id,
                "states": merged_states if reverse else merged_states[::-1],
                "lower_limit": lower_limit,
                "upper_limit": end_time,
            },
        )

    def __getitem__(self, index: int) -> State[S]:
        return self.states[index]

    def __len__(self) -> int:
        return len(self.states)

    def __iter__(self) -> Iterator[State[S]]:  # type: ignore[override]
        return iter(self.states)


class CosmoStateHistory(_History[CosmoState]):
    """A history of the vacuum's state."""


class CurrentRoomHistory(_History[Room]):
    """A history of the vacuum's current room."""


class TaskStatusHistory(_History[TaskStatus]):
    """A history of the vacuum's task status."""


class CosmoMonitor(Hass):  # type: ignore[misc]
    """Monitors the Cosmo vacuum's cleaning history."""

    def initialize(self) -> None:
        """Initialize the app."""
        # self.listen_state(self.go, "sensor.cosmo_task_status")   # noqa: ERA001

        clean_start_time, clean_end_time = self._get_cleaning_period()

        self.log("Cleaning period: %s to %s", clean_start_time, clean_end_time)

        room_history = CurrentRoomHistory.from_state_history(
            "sensor.cosmo_current_room",
            hass=self,
            lower_limit=clean_start_time,
            upper_limit=clean_end_time,
        )

        self.log(
            "Room history: %s",
            dumps(
                room_history,
                default=lambda x: (
                    x.model_dump() if isinstance(x, BaseModel) else str(x)
                ),
            ),
        )

    def _get_cleaning_period(self) -> tuple[datetime, datetime]:
        """Get the start and end times of the last cleaning period.

        Paused states are ignored when between two cleaning states.
        """
        # Start 6 hours ago because there's no way the battery will last that long
        six_hours_ago = datetime(
            2023,
            11,
            26,
            12,
            25,
            tzinfo=UTC,
        )  # self.datetime(tz=UTC) - timedelta(hours=6)

        task_history = TaskStatusHistory.from_state_history(
            "sensor.cosmo_task_status",
            hass=self,
            lower_limit=six_hours_ago,
            reverse=True,
        )

        # Work backwards through the task status history: take the first cleaning period and
        # save its end time; keep working backwards and saving the start time until a
        # non-cleaning/non-paused task is found
        clean_end_time = None
        for task_status_state in task_history:
            if task_status_state.state.is_room_cleaning():
                clean_end_time = clean_end_time or task_status_state.end_time
                clean_start_time = task_status_state.start_time
            elif not task_status_state.state.is_paused() and clean_end_time is not None:
                # Neither cleaning nor paused, and a cleaning period has been found
                break
        else:
            raise ValueError("No cleaning task status found")  # noqa: TRY003

        self.log("Initial cleaning period: %s to %s", clean_start_time, clean_end_time)

        # Trim the "returning to dock" time off the end
        cosmo_history = CosmoStateHistory.from_state_history(
            "vacuum.cosmo",
            hass=self,
            lower_limit=clean_start_time,
            upper_limit=clean_end_time,
            reverse=True,
        )

        # Work backwards through the vacuum entity's states, once a returning to dock state is found,
        # adjust the clean_end_time to be the start of that state
        for cosmo_state in cosmo_history:
            if cosmo_state.state == CosmoState.RETURNING_TO_DOCK:
                self.log(
                    f"Found returning to dock state at {cosmo_state.start_time!s}: {cosmo_state.model_dump_json()}",
                )
                clean_end_time = cosmo_state.start_time
                break

        return clean_start_time, clean_end_time

    def go(
        self,
        entity: Literal["sensor.cosmo_task_status"],
        attribute: Literal["state"],
        old: TaskStatus,
        new: TaskStatus,
        kwargs: dict[str, Any],
    ) -> None:
        """Deduces and records if a one or more rooms have been properly cleaned."""
        del entity, attribute, kwargs

        new, old = TaskStatus(new), TaskStatus(old)

        # Check it's gone from some type of room cleaning to completed
        if new != TaskStatus.COMPLETED or not old.is_room_cleaning():
            return

        # Get list of rooms cleaned in that time

        clean_start_time, clean_end_time = self._get_cleaning_period()

        _ = CurrentRoomHistory.from_state_history(
            "sensor.cosmo_current_room",
            hass=self,
            lower_limit=clean_start_time,
            upper_limit=clean_end_time,
        )

        # Update the input datetimes
