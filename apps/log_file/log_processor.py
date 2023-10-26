"""App to process the Home Assistant log."""


from __future__ import annotations

from hashlib import md5
from json import dumps
from logging import NOTSET, getLevelNamesMapping
from pathlib import Path
from typing import Final, Literal, TypedDict

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from wg_utilities.loggers import WarehouseHandler, add_warehouse_handler
from wg_utilities.loggers.item_warehouse.base_handler import LogPayload

FILE_PATH: Final[Path] = Path(__file__)


class SystemLogEvent(TypedDict):
    """Type for a system log event."""

    name: str
    message: list[str]
    level: str
    source: tuple[str, int]
    timestamp: float
    exception: str
    count: int
    first_occurred: float
    metadata: dict[str, str | dict[str, str]]


class LogProcessor(Hass):  # type: ignore[misc]
    """Class to process the Home Assistant logs."""

    warehouse_handler: WarehouseHandler

    LOG_FILE: Final[Path] = Path("/config/home-assistant.log")

    def initialize(self) -> None:
        """Initialize the app."""
        self.warehouse_handler = add_warehouse_handler(self.err)

        self.listen_event(self.process_log, "system_log_event")

        self.log("Listen event registered for `system_log_event`")

    def process_log(
        self,
        _: Literal["system_log_event"],
        data: SystemLogEvent,
        __: dict[str, str],
    ) -> None:
        """Catches `system_log_event` events and process the logs accordingly.

        Example Event:
            {
                "name": "homeassistant.components.tplink.coordinator",
                "message": [
                    "Error fetching 192.168.1.30 data: Unable to connect to the device: 192.168.1.30:9999: "
                ],
                "level": "ERROR",
                "source": [
                    "helpers/update_coordinator.py",
                    322
                ],
                "timestamp": 1697055033.9890194,
                "exception": "",
                "count": 1,
                "first_occurred": 1697055033.9890194,
                "metadata": {
                    "origin": "LOCAL",
                    "time_fired": "2023-10-11T20:10:33.993876+00:00",
                    "context": {
                        "id": "01HCG5SJM9QWTCMPCBTSP0AKCQ",
                        "parent_id": null,
                        "user_id": null
                    }
                }
            }

        Args:
            data (SystemLogEvent): the data from the event
        """
        self.log(dumps(data, indent=2))

        file, line = data["source"]

        exception_message = (
            data.get("exception", None) or None
        )  # account for empty string

        logger = data.get("name", "homeassistant") or "homeassistant"

        message = "\n".join(data.get("message", []))

        log_payload: LogPayload = {
            "created_at": data["timestamp"],
            "exception_message": exception_message,
            "exception_type": None,
            "exception_traceback": exception_message,
            "file": file,
            "level": getLevelNamesMapping().get(data.get("level", "INFO"), NOTSET),
            "line": line,
            "log_hash": md5(message.encode(), usedforsecurity=False).hexdigest(),
            "log_host": WarehouseHandler.HOST_NAME,
            "logger": logger,
            "message": message,
            "module": logger,
            "process": "-",
            "thread": "-",
        }

        self.warehouse_handler.post_with_backoff(log_payload)
