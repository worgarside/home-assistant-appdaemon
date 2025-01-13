"""Monitors the Cosmo vacuum's cleaning history."""

from __future__ import annotations

import operator
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum
from json import dumps
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    Generic,
    Literal,
    Self,
    TypedDict,
    TypeVar,
)

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-untyped]
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

UNAVAILABLE: Final[str] = "unavailable"


def is_timezone_aware(dttm: datetime, /) -> bool:
    """Return whether the datetime is timezone-aware."""
    return dttm.tzinfo is not None


class StateValue(StrEnum):
    """A state value."""


class CosmoState(StateValue):
    """A Cosmo vacuum state."""

    CLEANING = "cleaning"
    DOCKED = "docked"
    ERROR = "error"
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

    @property
    def is_room_cleaning(self) -> bool:
        """Return whether the task status is a room cleaning status."""
        return self in {
            TaskStatus.CLEANING,
            TaskStatus.ROOM_CLEANING,
            TaskStatus.ZONE_CLEANING,
        }

    @property
    def is_paused(self) -> bool:
        """Return whether the task status is a paused status."""
        return self in {
            TaskStatus.CLEANING_PAUSED,
            TaskStatus.DOCKING_PAUSED,
            TaskStatus.MAP_CLEANING_PAUSED,
            TaskStatus.ROOM_CLEANING_PAUSED,
        }


class Room(StateValue):
    """A room that Cosmo can vacuum."""

    BATHROOM = "Bathroom"
    BEDROOM = "Bedroom"
    EN_SUITE = "En-Suite"
    HALLWAY = "Hallway"
    KITCHEN = "Kitchen"
    LOUNGE = "Lounge"
    OFFICE = "Office"

    UNAVAILABLE = UNAVAILABLE

    @property
    def input_datetime_name(self) -> str:
        """Return the input_datetime name for the room."""
        return f"input_datetime.cosmo_last_{self.name.lower()}_clean"

    @property
    def minimum_clean_area(self) -> float:
        """Return the minimum area that should be cleaned before the room can be considered complete."""
        return {
            Room.BATHROOM: 3,
            Room.BEDROOM: 6,
            Room.EN_SUITE: 2,
            Room.HALLWAY: 7,
            Room.KITCHEN: 5,
            Room.LOUNGE: 8,
            Room.OFFICE: 5,
        }[self]


S = TypeVar("S", bound=StateValue | float)


class BaseStateTypeInfo(BaseModel, Generic[S]):
    """Information about a state type."""

    state: S


class HassStateTypeInfo(BaseStateTypeInfo[S]):
    """Information about a state type, straight from Home Assistant."""

    attributes: dict[str, Any]
    entity_id: str
    last_changed: AwareDatetime
    last_updated: AwareDatetime

    def __lt__(self, other: HassStateTypeInfo[S]) -> bool:
        """Return whether this state was updated before another state."""
        return self.last_updated < other.last_updated

    def __gt__(self, other: HassStateTypeInfo[S]) -> bool:
        """Return whether this state was updated after another state."""
        return self.last_updated > other.last_updated


class StateTypeInfo(BaseStateTypeInfo[S]):
    """Information about a state type, formatted for use in this app."""

    start_time: AwareDatetime
    end_time: AwareDatetime

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _prune_computed_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        for field in cls.model_computed_fields:
            data.pop(field, None)

        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration(self) -> timedelta:
        """Return the duration of the state."""
        return self.end_time - self.start_time

    def __str__(self) -> str:
        """Return a string representation of the state."""
        return f"{self.state}\t{self.start_time!s} - {self.end_time!s}"


class _History(BaseModel, Generic[S]):
    """A history of a state entity."""

    ENTITY_ID: ClassVar[str]

    states: list[StateTypeInfo[S]]
    state_history: Annotated[list[HassStateTypeInfo[S]], Field(exclude=True)]

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration(self) -> timedelta:
        """Return the total duration of the history."""
        return self.upper_limit - self.lower_limit

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lower_limit(self) -> AwareDatetime:
        """Return the lower limit of the history."""
        return self.states[0].start_time

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upper_limit(self) -> AwareDatetime:
        """Return the upper limit of the history."""
        return self.states[-1].end_time

    @field_validator("states", "state_history", mode="before")
    @classmethod
    def _filter_unavailable_states(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v

        return [s for s in v if s["state"] != UNAVAILABLE]

    @classmethod
    def _get_hass_state_history(
        cls: type[Self],
        *,
        hass: Hass,
        lower_limit: AwareDatetime,
        upper_limit: AwareDatetime | None = None,
    ) -> tuple[AwareDatetime, AwareDatetime, list[dict[str, Any]]]:
        if not is_timezone_aware(lower_limit):
            raise ValueError("Lower limit must be timezone-aware")

        if not is_timezone_aware(end_time := (upper_limit or hass.datetime(aware=True))):
            raise ValueError("End time must be timezone-aware")

        hass_history = hass.get_history(
            entity_id=cls.ENTITY_ID,
            start_time=lower_limit.astimezone(hass.local_tz).replace(tzinfo=None),
            end_time=end_time.astimezone(hass.local_tz).replace(tzinfo=None),
        )

        if not hass_history or len(hass_history) != 1:
            raise ValueError(
                f"Unexpected response from Home Assistant history API: {hass_history!r}",
            )

        state_history: list[dict[str, Any]] = sorted(  # Oldest -> newest
            hass_history[0],
            key=operator.itemgetter("last_updated"),
        )

        if state_history:
            hass.log(
                "Found %i state(s) for %s between %s and %s: %s",
                len(state_history),
                cls.ENTITY_ID,
                state_history[0]["last_changed"],
                upper_limit or "now",
                ", ".join(str(s["state"]) for s in state_history),
            )
        else:
            hass.log(
                "No states found for %s between %s and %s",
                cls.ENTITY_ID,
                lower_limit,
                upper_limit or "now",
            )

        return lower_limit, end_time, state_history

    @classmethod
    def from_state_history(
        cls: type[Self],
        hass: Hass,
        /,
        *,
        lower_limit: datetime,
        upper_limit: datetime | None = None,
        reverse: bool = False,
    ) -> Self:
        """Create a history from the Home Assistant history API.

        If `upper_limit` is None, then the current time is used.

        Args:
            hass: The Home Assistant instance.
            lower_limit: The lower limit of the history.
            upper_limit: The upper limit of the history. If None, the current time is used.
            reverse: Whether to reverse the history.

        Returns:
            History of the entity.
        """
        lower_limit, end_time, _state_history = cls._get_hass_state_history(
            hass=hass,
            lower_limit=lower_limit,
            upper_limit=upper_limit,
        )

        # This is a quick way to ensure that the state history is all parsed into the correct Pydantic
        # model without having to figure out the value of `S` at runtime
        parsed_state_history = cls.model_validate(
            {"states": [], "state_history": _state_history},
        ).state_history

        merged_states: list[StateTypeInfo[S]] = []
        while parsed_state_history:
            state = parsed_state_history.pop()  # Most recent state
            next_state = merged_states[-1] if merged_states else None

            if state.last_updated > end_time or cls.filter_current_state_out(
                prev_state=parsed_state_history[-1] if parsed_state_history else None,
                curr_state=state,
                next_state=next_state,
                hass=hass,
            ):
                # Ignore this state because it's after the upper limit
                continue

            # Bring last changed forward to lower limit if it's before it
            start_time = max(lower_limit, state.last_changed)

            # If this is not the first iteration/last state
            if next_state is not None:
                if next_state.state == state.state:
                    # If the previous iteration/next state has the same state, then set the start time to
                    # the start time of the previous iteration/next state
                    next_state.start_time = start_time
                    continue

                # Otherwise, set the end time to the start time of the previous iteration/next state
                end_time = next_state.start_time

            merged_states.append(
                StateTypeInfo(
                    start_time=start_time,
                    end_time=end_time,
                    state=state.state,
                ),
            )

            # If this state broke the lower limit (for the first time), then it has been rounded up and the
            # loop should be broken
            if state.last_updated < lower_limit:
                break

        if not reverse:
            # merged_states is already in reverse order, so only flip if reverse is False
            merged_states.reverse()

        return cls.model_validate(
            {
                "states": [m.model_dump() for m in merged_states],
                "state_history": [],
            },
        )

    @staticmethod
    def filter_current_state_out(
        *,
        prev_state: HassStateTypeInfo[S] | None,
        curr_state: HassStateTypeInfo[S],
        next_state: StateTypeInfo[S] | None,
        hass: Hass | None = None,
    ) -> bool:
        _ = prev_state, curr_state, next_state, hass
        return False

    def state_at(self, dttm: datetime, /) -> StateTypeInfo[S]:
        """Return the state at the given datetime."""
        for state in self.states:
            if state.start_time <= dttm < state.end_time:
                return state

        raise ValueError(
            f"No state found for {self.ENTITY_ID} at {dttm!s}",
        )

    def __getitem__(self, index: int) -> StateTypeInfo[S]:
        return self.states[index]

    def __len__(self) -> int:
        return len(self.states)

    def __iter__(self) -> Iterator[StateTypeInfo[S]]:  # type: ignore[override]
        return iter(self.states)

    def __str__(self) -> str:
        return "\n".join(str(state) for state in self.states)


class AreaCleanedHistory(_History[float]):
    """A history of the vacuum's cleaned area."""

    ENTITY_ID: ClassVar[str] = "sensor.cosmo_cleaned_area"

    @staticmethod
    def filter_current_state_out(
        *,
        prev_state: HassStateTypeInfo[float] | None,
        curr_state: HassStateTypeInfo[float],
        next_state: StateTypeInfo[float] | None,
        hass: Hass | None = None,
    ) -> bool:
        """Remove (usually) the first state of a cleaning session.

        The first state can be from the previous clean, in which case the change to 0.0 is the
        start of the current cleaning session.
        """
        _ = hass

        return (
            prev_state is None
            and next_state is not None
            and curr_state.state > 0  # Previous total area
            and next_state.state == 0  # The reset
        )


class CosmoStateHistory(_History[CosmoState]):
    """A history of the vacuum's state."""

    ENTITY_ID: ClassVar[str] = "vacuum.cosmo"


class CurrentRoomHistory(_History[Room]):
    """A history of the vacuum's current room."""

    ENTITY_ID: ClassVar[str] = "sensor.cosmo_current_room"

    @staticmethod
    def filter_current_state_out(
        *,
        prev_state: HassStateTypeInfo[Room] | None,
        curr_state: HassStateTypeInfo[Room],
        next_state: StateTypeInfo[Room] | None,
        hass: Hass | None = None,
    ) -> bool:
        """Filter out rooms Cosmo was in for < 10 seconds if the same room was visited either side.

        He was likely on a doorway and went too far.
        """
        _ = hass

        return (
            prev_state is not None
            and next_state is not None
            and prev_state.state == next_state.state
            and (
                (next_state.start_time - curr_state.last_updated) < timedelta(seconds=20)
            )
        )


class TaskStatusHistory(_History[TaskStatus]):
    """A history of the vacuum's task status."""

    ENTITY_ID: ClassVar[str] = "sensor.cosmo_task_status"


class AreaCleanedByRoom(TypedDict):
    """The area cleaned in a room."""

    area: float
    end_time: datetime


class CosmoMonitor(Hass):  # type: ignore[misc]
    """Monitors the Cosmo vacuum's cleaning history."""

    local_tz: tzinfo

    def initialize(self) -> None:
        """Initialize the app."""
        self.listen_state(self.log_cleaning_time, "sensor.cosmo_task_status")

        self.local_tz = self.datetime(aware=True).astimezone().tzinfo

        # Call once when app loads (in case of a bugfix for a prior cleaning session)
        self.log_cleaning_time(
            entity="sensor.cosmo_task_status",
            attribute="state",
            old=TaskStatus.ROOM_CLEANING,
            new=TaskStatus.COMPLETED,
            kwargs={},
        )

    def _get_area_cleaned_by_room(
        self,
        *,
        room_history: CurrentRoomHistory,
        area_cleaned_history: AreaCleanedHistory,
    ) -> dict[Room, AreaCleanedByRoom]:
        """Return the area cleaned in each room."""
        area_cleaned_by_room: dict[Room, AreaCleanedByRoom] = {}

        prev_value = None

        for area_state in area_cleaned_history:
            if area_state.state not in {None, 0.0, prev_value}:
                room_state = room_history.state_at(area_state.start_time)

                area_cleaned_by_room.setdefault(
                    room_state.state,
                    {
                        "area": 0.0,
                        "end_time": area_state.end_time,
                    },
                )

                area_cleaned_by_room[room_state.state]["area"] += area_state.state - (
                    prev_value or 0
                )

                area_cleaned_by_room[room_state.state]["end_time"] = area_state.end_time

            prev_value = area_state.state

        self.log(
            "Area cleaned by room: %s",
            dumps(area_cleaned_by_room, default=str),
        )

        return area_cleaned_by_room

    def _get_cleaning_period(self) -> tuple[datetime, datetime]:
        """Get the start and end times of the last cleaning period.

        Paused states are ignored when between two cleaning states.
        """
        # Start 24 hours ago because there's no way a cleaning session will last that long
        task_history = TaskStatusHistory.from_state_history(
            self,
            lower_limit=self.datetime(aware=True).astimezone(self.local_tz)
            - timedelta(hours=24),
            reverse=True,
        )

        # Work backwards through the task status history: take the first cleaning period and
        # save its end time; keep working backwards and saving the start time until a
        # non-cleaning/non-paused task is found
        clean_end_time = None
        for task_status_state in task_history:
            if task_status_state.state.is_room_cleaning:
                clean_end_time = clean_end_time or task_status_state.end_time
                clean_start_time = task_status_state.start_time
            elif not task_status_state.state.is_paused and clean_end_time is not None:
                # Neither cleaning nor paused, and a cleaning period has been found
                break
        else:
            raise ValueError("No cleaning task status found")

        self.log("Initial range found: %s - %s", clean_start_time, clean_end_time)

        cosmo_history = CosmoStateHistory.from_state_history(
            self,
            lower_limit=clean_start_time,
            upper_limit=clean_end_time,
            reverse=True,
        )

        # Work backwards through the vacuum entity's states, once a cleaning state is found,
        # adjust the clean_end_time to be the end of that state
        for cosmo_state in cosmo_history:
            if cosmo_state.state == CosmoState.CLEANING:
                clean_end_time = cosmo_state.end_time
                break

        self.log(
            "Range refined from vacuum.cosmo history: %s - %s",
            clean_start_time,
            clean_end_time,
        )

        return clean_start_time, clean_end_time

    def log_cleaning_time(
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
        if new != TaskStatus.COMPLETED or not (old.is_room_cleaning or old.is_paused):
            self.log(
                "Not a room cleaning: %s -> %s",
                old,
                new,
            )
            return

        # Get list of rooms cleaned in that time

        clean_start_time, clean_end_time = self._get_cleaning_period()

        self.log(
            "Last cleaning period: %s - %s",
            clean_start_time.strftime("%Y-%m-%d %H:%M:%S.%f%z"),
            clean_end_time.strftime("%Y-%m-%d %H:%M:%S.%f%z"),
        )

        room_history = CurrentRoomHistory.from_state_history(
            self,
            lower_limit=clean_start_time,
            upper_limit=clean_end_time,
        )

        # Get the area cleaned in that time
        area_cleaned_history = AreaCleanedHistory.from_state_history(
            self,
            lower_limit=clean_start_time,
            upper_limit=clean_end_time,
        )

        area_cleaned_by_room = self._get_area_cleaned_by_room(
            room_history=room_history,
            area_cleaned_history=area_cleaned_history,
        )

        for room, room_stats in area_cleaned_by_room.items():
            if (area_cleaned := room_stats["area"]) >= room.minimum_clean_area:
                self.log(
                    "%s has been cleaned enough (%.2f m²)",
                    room,
                    area_cleaned,
                )
                self.call_service(
                    "input_datetime/set_datetime",
                    entity_id=room.input_datetime_name,
                    datetime=room_stats["end_time"].isoformat(),
                )
            else:
                self.log(
                    "%s has not been cleaned enough (%.2f m² cleaned, %.2f m² required)",
                    room,
                    area_cleaned,
                    room.minimum_clean_area,
                )
