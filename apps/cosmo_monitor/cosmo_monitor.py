from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from json import dumps
from typing import Any, ClassVar, Final, Generic, Literal, TypedDict, TypeVar

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from pydantic import BaseModel, ConfigDict, computed_field

UNAVAILABLE: Final[str] = "unavailable"


class StateValue(StrEnum):
    """A state value."""


class CosmoState(StateValue):
    # TODO add rest of states
    CLEANING = "cleaning"
    DOCKED = "docked"
    IDLE = "idle"
    PAUSED = "paused"
    RETURNING_TO_DOCK = "returning"

    UNAVAILABLE = UNAVAILABLE


class TaskStatus(StateValue):
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
        return self in (
            TaskStatus.CLEANING,
            TaskStatus.ROOM_CLEANING,
            TaskStatus.ZONE_CLEANING,
        )

    def is_paused(self) -> bool:
        return self in (
            TaskStatus.CLEANING_PAUSED,
            TaskStatus.DOCKING_PAUSED,
            TaskStatus.MAP_CLEANING_PAUSED,
            TaskStatus.ROOM_CLEANING_PAUSED,
        )


class Room(StateValue):
    BEDROOM = "Bedroom"
    EN_SUITE = "En-Suite"
    HALLWAY = "Hallway"
    KITCHEN = "Kitchen"
    LOUNGE = "Lounge"
    OFFICE = "Office"

    UNAVAILABLE = UNAVAILABLE


T = TypeVar("T", bound=StateValue)


class StateTypeInfo(TypedDict, Generic[T]):
    """Information about a state type."""

    entity_id: str
    start_time: datetime
    end_time: datetime
    state: T
    duration: timedelta
    last_changed: str
    last_updated: str


class State(BaseModel, Generic[T]):
    start_time: datetime
    end_time: datetime
    state: T

    model_config: ClassVar[ConfigDict] = {"extra": "ignore"}

    @computed_field  # type: ignore[misc]
    @property
    def duration(self) -> timedelta:
        return self.end_time - self.start_time

    def __str__(self) -> str:
        return (
            f"{self.entity_id}\t{self.state}\t {self.start_time!s} to {self.end_time!s}"
        )


class _History(BaseModel, Generic[T]):
    entity_id: str
    states: list[State[T]]

    @classmethod
    def from_state_history(
        cls,
        entity_id: str,
        /,
        hass: Hass,
        *,
        lower_limit: datetime,
        upper_limit: datetime | None = None,
        remove_unavailable_states: bool = True,
        reverse: bool = False,
    ) -> _History[T]:
        end_time = (upper_limit.astimezone(UTC).replace(tzinfo=None)
            if upper_limit
            else datetime.utcnow())

        hass_history = hass.get_history(
            entity_id=entity_id,
            start_time=lower_limit.astimezone(UTC).replace(tzinfo=None),
            end_time=end_time,
        )

        if not hass_history or len(hass_history) != 1:
            raise ValueError(
                f"Unexpected response from Home Assistant history API: {hass_history!r}"
            )

        state_history: list[StateTypeInfo[T]] = sorted( # Oldest -> newest
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

        hass.log(dumps(state_history[:5], default=str, sort_keys=True))


        merged_states = [] # Descending order (newest -> oldest)
        while state_history:
            state = state_history.pop() # Most recent state

            if state["last_changed"] > end_time:
                # Ignore this state because it's after the upper limit
                continue

            # If adjacent states are the same, merge them by bringing the start time of the previous
            # state/next iteration forward to the start time of the current state
            if (previous_state := state_history[-1] if state_history else None):
                if state["last_changed"] == previous_state["last_changed"]:
                    # Ignore this state because it's a duplicate
                    continue


            if lower_limit_broken:=((last_changed := datetime.fromisoformat(state["last_changed"])) < lower_limit):
                # Bring last changed forward to lower limit if it's before it
                state["last_changed"] = lower_limit
            else:
                state["last_changed"] = last_changed

            if not merged_states:
                # If this is the first iteration/last state
                state["end_time"] = end_time
            else:
                # Otherwise, set the end time to the start time of the previous iteration/next state
                state["end_time"] = merged_states[-1]["start_time"]

            merged_states.append({
                "start_time": state["last_changed"],
                "end_time": state["end_time"],
                "state": state["state"],
            })

            # If this state broke the lower limit (for the first time), then it has been rounded up and the
            # loop should be broken
            if lower_limit_broken:
                break

        if not remove_unavailable_states:
            # merged_states is already in reverse order, so only flip if reverse is False
            return cls.model_validate(dict(entity_id=entity_id,states=(merged_states if reverse else merged_states[::-1])))

        del state, state_history

        filtered_state_history = [] # Ascending order (oldest -> newest)
        while merged_states: # Descending order (newest -> oldest)
            state = merged_states.pop() # Oldest state

            # If the state is valid, add it to the list and move on
            if state["state"] != UNAVAILABLE:
                filtered_state_history.append(state)
                continue

            # If 'unavailable' is the last item, expand the previous item
            if not merged_states:  # Because the final state was popped off the list
                if filtered_state_history:
                    filtered_state_history[-1]["end_time"] = state["end_time"]
                continue

            # If 'unavailable' is the first item, expand the next item
            if not filtered_state_history:  # Because no states have been processed yet
                # Don't need to check if state_history is empty because of the previous check
                merged_states[0]["start_time"] = state["start_time"]
                continue

            # If 'unavailable' is in the middle of two identical items
            if (
                filtered_state_history[-1]["state"]
                == (next_state := state_history[-1])["state"]
            ):
                # Merge all three states
                filtered_state_history[-1]["end_time"] = next_state["end_time"]
                state_history.pop()  # Discard the next state
                continue

            # If 'unavailable' is in the middle of two different items
            middle_time = state["start_time"] + (
                (state["end_time"] - state["start_time"]) / 2
            )

            filtered_state_history[-1]["end_time"] = middle_time
            merged_states[-1]["start_time"] = middle_time

        # Don't need to reverse because the states were appended in chronological order
        return cls.model_validate(dict(entity_id=entity_id,
            states=(filtered_state_history[::-1] if reverse else filtered_state_history)
        ))

    def __getitem__(self, index: int) -> State[T]:
        return self.states[index]

    def __len__(self) -> int:
        return len(self.states)

    def __iter__(self) -> Iterator[State[T]]:  # type: ignore[override]
        return iter(self.states)


class CosmoStateHistory(_History[CosmoState]):
    """A history of the vacuum's state."""


class CurrentRoomHistory(_History[Room]):
    """A history of the vacuum's current room."""


class TaskStatusHistory(_History[TaskStatus]):
    """A history of the vacuum's task status."""


class CosmoMonitor(Hass):  # type: ignore[misc]
    def initialize(self) -> None:
        """Initialize the app."""

        # self.listen_state(self.go, "sensor.cosmo_task_status")

        clean_start_time, clean_end_time = self._get_cleaning_period()

        self.log("Cleaning period: %s to %s", clean_start_time, clean_end_time)

        # room_history = CurrentRoomHistory.from_state_history(
        #     "sensor.cosmo_current_room",
        #     hass=self,
        #     lower_limit=clean_start_time,
        #     upper_limit=clean_end_time,
        # )

    def _get_cleaning_period(self) -> tuple[datetime, datetime]:
        """Get the start and end times of the last cleaning period.

        Paused states are ignored when between two cleaning states.
        """
        # Start 6 hours ago because there's no way the battery will last that long
        six_hours_ago = datetime(
            2023, 11, 26, 12, 25, tzinfo=UTC
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
            raise ValueError("No cleaning task status found")

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
                    f"Found returning to dock state at {cosmo_state.start_time!s}: {cosmo_state.model_dump_json()}"
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

        new, old = TaskStatus(new), TaskStatus(old)

        # Check it's gone from some type of room cleaning to completed
        if new != TaskStatus.COMPLETED or not old.is_room_cleaning():
            return

        # Get list of rooms cleaned in that time

        clean_start_time, clean_end_time = self._get_cleaning_period()

        room_history = CurrentRoomHistory.from_state_history(
            "sensor.cosmo_current_room",
            hass=self,
            lower_limit=clean_start_time,
            upper_limit=clean_end_time,
        )

        # Update the input datetimes
