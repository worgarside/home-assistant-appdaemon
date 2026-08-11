"""Atomic JSON persistence for habit configuration and history."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .models import SCHEMA_VERSION, MoodCheckIn, StoreData, UnsupportedSchemaVersionError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class HabitStore:
    """Own a recoverable, atomically-written habit store."""

    def __init__(
        self,
        directory: Path,
        users: tuple[str, ...],
        *,
        log: Callable[[str], None],
        error: Callable[[str], None],
    ) -> None:
        self.directory = directory
        self.path = directory / "store.json"
        self.backup_path = directory / "store.json.backup"
        self.users = users
        self._log = log
        self._error = error
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> StoreData:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        saw_unreadable = False
        for candidate in (self.path, self.backup_path):
            try:
                data = self._read(candidate)
            except FileNotFoundError:
                continue
            except UnsupportedSchemaVersionError as exc:
                # Intact data written by a different build. Quarantining would
                # rename it and the next save would replace it with an empty
                # store, so refuse to start and leave the file untouched.
                self._error(
                    f"Habit store {candidate} was written by an incompatible "
                    f"build ({exc}); refusing to start so the file is not "
                    "overwritten. Restore a compatible store or upgrade the app",
                )
                raise
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                saw_unreadable = True
                self._error(
                    "Habit store candidate "
                    f"{candidate} is unreadable ({type(exc).__name__}: {exc}); "
                    "quarantining",
                )
                self._quarantine(candidate)
            else:
                self._preserve_pre_migration(candidate)
                for user in self.users:
                    data.users.setdefault(user, StoreData.empty((user,)).users[user])
                return data
        if saw_unreadable:
            self._error(
                "No readable habit store found (primary and backup exhausted); "
                "starting with an empty store — habit configs and history may "
                "have been lost",
            )
        else:
            self._log("No habit store found; starting with an empty store")
        return StoreData.empty(self.users)

    def _preserve_pre_migration(self, path: Path) -> None:
        """Keep one immutable copy of a valid older schema before first save."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            if not isinstance(version, int) or version >= SCHEMA_VERSION:
                return
            destination = self.directory / f"store.json.schema-v{version}-backup"
            if destination.exists():
                return
            shutil.copy2(path, destination)
            destination.chmod(0o600)
            self._log(f"Preserved pre-migration habit store at {destination}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._error(f"Could not preserve pre-migration habit store: {exc}")

    @staticmethod
    def _read(path: Path) -> StoreData:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("store root must be an object")
        return StoreData.from_dict(
            {str(key): value for key, value in payload.items()},
        )

    def save(self) -> None:
        """Atomically persist current data and retain the previous valid version."""
        with self._lock:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = json.dumps(
                self.data.to_dict(),
                indent=2,
                sort_keys=True,
            )
            temporary = self.directory / f".store-{os.getpid()}-{uuid.uuid4().hex}.tmp"
            temporary.write_text(f"{payload}\n", encoding="utf-8")
            temporary.chmod(0o600)
            with temporary.open("r+", encoding="utf-8") as file:
                file.flush()
                os.fsync(file.fileno())
            if self.path.exists():
                shutil.copy2(self.path, self.backup_path)
            temporary.replace(self.path)
            self.path.chmod(0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _quarantine(self, path: Path) -> None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = path.with_name(f"{path.name}.invalid-{timestamp}")
        try:
            path.replace(destination)
        except OSError as exc:
            self._error(f"Failed to quarantine habit store {path}: {exc}")
            return
        self._error(f"Quarantined unreadable habit store at {destination}")

    def append_completion_log(
        self,
        user: str,
        slot: int,
        day: str,
        count: int,
    ) -> None:
        """Append a durable audit record after a completion mutation."""
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": user,
            "slot": slot,
            "date": day,
            "count": count,
        }
        path = self.directory / "completions.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, sort_keys=True))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        path.chmod(0o600)

    def append_mood_log(self, user: str, operation: str, checkin: MoodCheckIn) -> None:
        """Append an audit record after a mood mutation."""
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user": user,
            "operation": operation,
            "checkin": checkin.to_dict(),
        }
        path = self.directory / "mood-checkins.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, sort_keys=True))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        path.chmod(0o600)
