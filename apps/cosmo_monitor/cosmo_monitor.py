from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, timedelta
from enum import StrEnum
from json import dumps
from typing import Any, ClassVar, Generic, Literal, TypedDict, TypeVar

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from pydantic import BaseModel, ConfigDict, Field, computed_field
from tzlocal import get_localzone


class StateValue(StrEnum):
    """A state value."""


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

    UNAVAILABLE = "unavailable"

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

    UNAVAILABLE = "unavailable"


T = TypeVar("T", bound=StateValue)


class StateTypeInfo(TypedDict, Generic[T]):
    """Information about a state type."""

    entity_id: str
    start_time: datetime
    end_time: datetime
    state: T
    duration: timedelta
    last_updated: datetime


class State(BaseModel, Generic[T]):
    entity_id: str
    start_time: datetime = Field(validation_alias="last_updated")
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
    states: list[State[T]]

    @classmethod
    def from_state_history(
        cls,
        entity_id: str,
        /,
        start_time: datetime,
        *,
        hass: Hass,
        remove_unavailable_states: bool = True,
        reverse: bool = False,
        lower_limit: datetime = datetime.min,
        upper_limit: datetime = datetime.max,
    ) -> _History[T]:
        state_history: list[StateTypeInfo[T]] = sorted(
            hass.get_history(
                entity_id=entity_id, start_time=start_time  # .replace(tzinfo=None),
            )[0],
            key=lambda x: x["last_updated"],
            reverse=True,
        )

        for i, state in enumerate(state_history):
            state["start_time"] = datetime.fromisoformat(state["start_time"])  # type: ignore[arg-type]

            if i == 0:
                state["end_time"] = datetime.now(tz=get_localzone())
                continue

            state_history[i + 1]["end_time"] = state["last_updated"]

            if i == len(state_history) - 1:
                break

        filtered_state_history = []
        if remove_unavailable_states:
            while state_history:
                current = state_history.pop()

                if (
                    current["end_time"] < lower_limit
                    or current["start_time"] > upper_limit
                ):
                    continue

                current["start_time"] = max(current["start_time"], lower_limit)
                current["end_time"] = min(current["end_time"], upper_limit)

                # If the state is valid, add it to the list and move on
                if current["state"] != Room.UNAVAILABLE:
                    filtered_state_history.append(current)
                    continue

                # If 'unavailable' is the last item, expand the previous item
                if not state_history:  # Because the last state was popped off the list
                    if filtered_state_history:
                        filtered_state_history[-1]["end_time"] = current["end_time"]
                    continue

                # If 'unavailable' is the first item, expand the next item
                if (
                    not filtered_state_history
                ):  # Because no states have been processed yet
                    # Don't need to check if state_history is empty because of the previous check
                    state_history[0]["start_time"] = current["start_time"]
                    continue

                # If 'unavailable' is in the middle of two identical items
                if (
                    filtered_state_history[-1]["state"]
                    == (next_state := state_history[-1])["state"]
                ):
                    # Merge all three states
                    filtered_state_history[-1]["end_time"] = next_state["end_time"]
                    state_history.pop()
                    continue

                # If 'unavailable' is in the middle of two different items
                middle_time = current["start_time"] + (
                    (current["end_time"] - current["start_time"]) / 2
                )

                filtered_state_history[-1]["end_time"] = middle_time
                state_history[-1]["start_time"] = middle_time

        return cls.model_validate({"states": state_history or filtered_state_history})

    def __iter__(self) -> Iterator[State[T]]:  # type: ignore[override]
        return iter(self.states)


class CosmoStateHistory(_History[StateValue]):
    """A history of the vacuum's state."""


class CurrentRoomHistory(_History[Room]):
    """A history of the vacuum's current room."""


class TaskStatusHistory(_History[TaskStatus]):
    """A history of the vacuum's task status."""


class CosmoMonitor(Hass):  # type: ignore[misc]
    def initialize(self) -> None:
        """Initialize the app."""

        # self.listen_state(self.go, "sensor.cosmo_task_status")

        start, end = self._get_cleaning_period()

        rooms = self._get_rooms_cleaned(start, end)

        self.log(
            dumps(
                rooms,
                default=lambda x: str(x)
                if not isinstance(x, BaseModel)
                else x.model_dump(),
            )
        )

    def _get_cleaning_period(self) -> tuple[datetime, datetime]:
        # Start 6 hours ago because thewree's no way the battery will last that long
        six_hours_ago = datetime(2023, 11, 26)  # self.datetime() - timedelta(hours=6)

        task_history = TaskStatusHistory.from_state_history(
            "sensor.cosmo_task_status",
            hass=self,
            start_time=six_hours_ago,
            reverse=True,
        )

        clean_end_time = None

        for task_status_state in task_history:
            if task_status_state.state.is_room_cleaning():
                clean_end_time = clean_end_time or task_status_state.end_time
                clean_start_time = task_status_state.start_time
            elif not task_status_state.state.is_paused() and clean_end_time is not None:
                break
        else:
            raise ValueError("No cleaning state found")

        # Trim the "returning to dock" time off the end
        cosmo_history = CosmoStateHistory.from_state_history(
            "vacuum.cosmo",
            start_time=clean_start_time.replace(tzinfo=None),
            hass=self,
            reverse=True,
        )

        # for cosmo_state in cosmo_history:
        #     if

        self.log(
            f"Cleaning period: {clean_start_time!s} to {clean_end_time!s} (duration: {clean_end_time - clean_start_time})"
        )

        return clean_start_time, clean_end_time

    def _get_rooms_cleaned(
        self, clean_start_time: datetime, clean_end_time: datetime
    ) -> list[State[Room]]:
        history = CurrentRoomHistory.from_state_history(
            "sensor.cosmo_current_room",
            start_time=clean_start_time.replace(tzinfo=None),
            hass=self,
            reverse=True,
        )

        self.log(
            dumps(
                history,
                default=lambda x: str(x)
                if not isinstance(x, BaseModel)
                else x.model_dump(),
            )
        )

        return history.states

        processed_states = []

        states = deepcopy(history.states)

        while states:
            current = states.pop()

            if (
                current.end_time < clean_start_time
                or current.start_time > clean_end_time
            ):
                continue

            current.start_time = max(current.start_time, clean_start_time)
            current.end_time = min(current.end_time, clean_end_time)

            # If the state is valid, add it to the list and move on
            if current.state != Room.UNAVAILABLE:
                # if processed_states and processed_states[-1].state == current.state:
                # processed_states[-1].end_time = current.end_time
                # else:
                processed_states.append(current)

                continue

            # If 'unavailable' is the last item, expand the previous item
            if not states:  # Because the last state was popped off the list
                if processed_states:
                    processed_states[-1].end_time = current.end_time
                continue

            # If 'unavailable' is the first item, expand the next item
            if not processed_states:  # Because no states have been processed yet
                # Don't need to check if states is empty because of the previous check
                states[0].start_time = current.start_time
                continue

            # If 'unavailable' is in the middle of two identical items
            if processed_states[-1].state == (next_state := states[-1]).state:
                # Merge all three states
                processed_states[-1].end_time = next_state.end_time
                states.pop()
                continue

            # If 'unavailable' is in the middle of two different items
            middle_time = current.start_time + (current.duration / 2)

            processed_states[-1].end_time = middle_time
            states[-1].start_time = middle_time

        return processed_states

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

        rooms = self._get_rooms_cleaned(clean_start_time, clean_end_time)

        # Update the input datetimes
