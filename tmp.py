from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from json import dumps, loads
from typing import Any, ClassVar, Final, Generic, Literal, TypedDict, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field
from requests import post
from tzlocal import get_localzone

UNAVAILABLE: Final[str] = "unavailable"


_HISTORY = {
    "sensor.cosmo_task_status": [
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 0,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-27T09:44:24.354219+00:00",
            "last_updated": "2023-11-27T09:44:24.354219+00:00",
            "state": "completed",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 0,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-27T08:09:19.348562+00:00",
            "last_updated": "2023-11-27T08:09:19.348562+00:00",
            "state": "completed",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 4,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-27T08:05:23.700406+00:00",
            "last_updated": "2023-11-27T08:05:23.700406+00:00",
            "state": "spot_cleaning",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 0,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T12:40:22.635031+00:00",
            "last_updated": "2023-11-26T12:40:22.635031+00:00",
            "state": "completed",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 3,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T12:25:02.416309+00:00",
            "last_updated": "2023-11-26T12:25:02.416309+00:00",
            "state": "room_cleaning",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 8,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T12:24:52.297921+00:00",
            "last_updated": "2023-11-26T12:24:52.297921+00:00",
            "state": "room_cleaning_paused",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 3,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T12:21:40.800699+00:00",
            "last_updated": "2023-11-26T12:21:40.800699+00:00",
            "state": "room_cleaning",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 0,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T12:18:42.093173+00:00",
            "last_updated": "2023-11-26T12:18:42.093173+00:00",
            "state": "completed",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 4,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T12:16:03.539621+00:00",
            "last_updated": "2023-11-26T12:16:03.539621+00:00",
            "state": "spot_cleaning",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 0,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T12:14:40.023412+00:00",
            "last_updated": "2023-11-26T12:14:40.023412+00:00",
            "state": "completed",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 3,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T11:49:06.168878+00:00",
            "last_updated": "2023-11-26T11:49:06.168878+00:00",
            "state": "room_cleaning",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 0,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T11:48:32.376151+00:00",
            "last_updated": "2023-11-26T11:48:32.376151+00:00",
            "state": "completed",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 8,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T11:48:27.239294+00:00",
            "last_updated": "2023-11-26T11:48:27.239294+00:00",
            "state": "room_cleaning_paused",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 3,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T11:43:45.952990+00:00",
            "last_updated": "2023-11-26T11:43:45.952990+00:00",
            "state": "room_cleaning",
        },
        {
            "attributes": {
                "device_class": "dreame_vacuum__task_status",
                "friendly_name": "Cosmo Task Status",
                "icon": "mdi:file-tree",
                "value": 0,
            },
            "entity_id": "sensor.cosmo_task_status",
            "last_changed": "2023-11-26T11:25:00+00:00",
            "last_updated": "2023-11-26T11:25:00+00:00",
            "state": "completed",
        },
    ]
}


def main(
    lower_limit: datetime = datetime(2023, 11, 26, 12, 25, tzinfo=UTC),
    upper_limit: datetime = datetime.max.replace(tzinfo=UTC),
):
    state_history = sorted(
        _HISTORY["sensor.cosmo_task_status"],
        key=lambda x: x["last_updated"],
    )
    return state_history

    for i, state in enumerate(state_history):
        state["start_time"] = max(datetime.fromisoformat(state["last_changed"]), lower_limit).astimezone(UTC).replace(tzinfo=UTC)  # type: ignore[arg-type]

        if i == 0:
            state["end_time"] = min(datetime.now(tz=UTC), upper_limit)

        try:
            state_history[i + 1]["end_time"] = min(state["start_time"], upper_limit)
        except IndexError:
            break


    filtered_state_history = []
    while state_history:
        current = state_history.pop()

        if current["end_time"] < lower_limit or current["start_time"] > upper_limit:
            continue

        # If the state is valid, add it to the list and move on
        if current["state"] != UNAVAILABLE:
            filtered_state_history.append(current)
            continue

        # If 'unavailable' is the last item, expand the previous item
        if not state_history:  # Because the final state was popped off the list
            if filtered_state_history:
                filtered_state_history[-1]["end_time"] = current["end_time"]
            continue

        # If 'unavailable' is the first item, expand the next item
        if not filtered_state_history:  # Because no states have been processed yet
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
            state_history.pop()  # Discard the next state
            continue

        # If 'unavailable' is in the middle of two different items
        middle_time = current["start_time"] + (
            (current["end_time"] - current["start_time"]) / 2
        )

        filtered_state_history[-1]["end_time"] = middle_time
        state_history[-1]["start_time"] = middle_time

    return filtered_state_history


if __name__ == "__main__":
    print(dumps(main(), indent=2, default=str))
