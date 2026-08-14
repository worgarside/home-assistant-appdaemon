"""Offer deletion of the seeding torrent closest to its share limit."""

from __future__ import annotations

from dataclasses import dataclass
from re import compile as compile_regex
from typing import Any, Final

from appdaemon.plugins.hass.hassapi import Hass
from requests import RequestException, Response, Session

BYTES_PER_UNIT: Final = 1024
SECONDS_PER_MINUTE: Final = 60
USE_GLOBAL_LIMIT: Final = -2.0
ACTION_PREFIX: Final = "DELETE_QBT_TORRENT_"
TORRENT_HASH_PATTERN: Final = compile_regex(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class QbittorrentError(RuntimeError):
    """Raised when qBittorrent cannot complete an API operation."""


@dataclass(frozen=True, slots=True)
class TorrentCandidate:
    """A seeding torrent and its progress towards automatic removal."""

    hash: str
    name: str
    size: int
    ratio: float
    ratio_limit: float | None
    upload_speed: int
    seeding_seconds: int
    time_limit_seconds: float | None
    closeness: float
    deletion_score: float
    closest_limit: str

    @property
    def size_formatted(self) -> str:
        """Return the torrent size using IEC units."""
        amount = float(self.size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if amount < BYTES_PER_UNIT or unit == "TiB":
                precision = 0 if unit == "B" else 1
                return f"{amount:.{precision}f} {unit}"
            amount /= BYTES_PER_UNIT
        return f"{self.size} B"

    @property
    def limit_summary(self) -> str:
        """Describe the limit that currently puts the torrent closest to removal."""
        progress = f"{self.closeness * 100:.1f}%"
        if self.closest_limit == "ratio" and self.ratio_limit is not None:
            return f"{progress} of its {self.ratio_limit:g} ratio limit"
        if self.time_limit_seconds is not None:
            days = self.time_limit_seconds / (24 * 60 * 60)
            return f"{progress} of its {days:g}-day seeding limit"
        return f"{progress} towards its share limit"

    @property
    def is_uploading(self) -> bool:
        """Return whether qBittorrent reports active upload traffic."""
        return self.upload_speed > 0


class QbittorrentWebApi:
    """Small authenticated client for the qBittorrent Web API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        ratio_progress_weight: float,
        timeout: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.ratio_progress_weight = ratio_progress_weight
        self.timeout = timeout

    def ranked_seeders(self) -> list[TorrentCandidate]:
        """Return eligible seeders ordered by proximity to a share limit."""
        with self._authenticated_session() as session:
            preferences = self._json_object(
                self._request(session, "GET", "/api/v2/app/preferences"),
            )
            torrents = self._json_list(
                self._request(
                    session,
                    "GET",
                    "/api/v2/torrents/info",
                    params={"filter": "seeding"},
                ),
            )
        return rank_seeders(
            torrents,
            preferences,
            ratio_progress_weight=self.ratio_progress_weight,
        )

    def delete_with_files(self, torrent_hash: str) -> None:
        """Delete one exact torrent and its downloaded content."""
        with self._authenticated_session() as session:
            self._request(
                session,
                "POST",
                "/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": "true"},
            )

    def _authenticated_session(self) -> Session:
        session = Session()
        session.headers.update({"Referer": f"{self.base_url}/"})
        try:
            response = self._request(
                session,
                "POST",
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
        except Exception:
            session.close()
            raise
        if response.text.strip() != "Ok.":
            session.close()
            raise QbittorrentError("qBittorrent rejected the configured credentials")
        return session

    def _request(
        self,
        session: Session,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Response:
        try:
            response = session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
        except RequestException as error:
            raise QbittorrentError(f"qBittorrent API request failed: {error}") from error
        return response

    @staticmethod
    def _json_object(response: Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise QbittorrentError("qBittorrent returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise QbittorrentError("qBittorrent preferences response was not an object")
        return payload

    @staticmethod
    def _json_list(response: Response) -> list[dict[str, Any]]:
        try:
            payload = response.json()
        except ValueError as error:
            raise QbittorrentError("qBittorrent returned invalid JSON") from error
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise QbittorrentError("qBittorrent torrents response was not a list")
        return payload


def _as_float(value: Any, default: float = 0.0) -> float:
    """Convert an API value to a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    """Convert an API value to an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _effective_limit(
    torrent_limit: Any,
    global_limit: Any,
    *,
    global_enabled: bool,
) -> float | None:
    """Resolve a per-torrent ratio limit against the global preference."""
    limit = _as_float(torrent_limit, USE_GLOBAL_LIMIT)
    if limit == USE_GLOBAL_LIMIT:
        limit = _as_float(global_limit, -1) if global_enabled else -1
    return limit if limit > 0 else None


def _effective_time_limit_seconds(
    torrent_limit: Any,
    global_limit_minutes: Any,
    *,
    global_enabled: bool,
) -> float | None:
    """Resolve a per-torrent seeding limit to seconds."""
    limit_minutes = _as_float(torrent_limit, USE_GLOBAL_LIMIT)
    if limit_minutes == USE_GLOBAL_LIMIT:
        if not global_enabled:
            return None
        limit_minutes = _as_float(global_limit_minutes, -1)
    return limit_minutes * SECONDS_PER_MINUTE if limit_minutes > 0 else None


def rank_seeders(
    torrents: list[dict[str, Any]],
    preferences: dict[str, Any],
    *,
    ratio_progress_weight: float = 1.0,
) -> list[TorrentCandidate]:
    """Rank seeders by weighted ratio progress or unweighted time progress."""
    global_ratio_enabled = bool(preferences.get("max_ratio_enabled", True))
    global_time_enabled = bool(preferences.get("max_seeding_time_enabled", True))
    global_ratio_limit = preferences.get("max_ratio", -1)
    global_time_limit = preferences.get("max_seeding_time", -1)

    ranked: list[TorrentCandidate] = []
    for torrent in torrents:
        torrent_hash = str(torrent.get("hash", "")).lower()
        if not TORRENT_HASH_PATTERN.fullmatch(torrent_hash):
            continue

        ratio = max(_as_float(torrent.get("ratio")), 0.0)
        seeding_seconds = max(_as_int(torrent.get("seeding_time")), 0)
        ratio_limit = _effective_limit(
            torrent.get("ratio_limit", USE_GLOBAL_LIMIT),
            global_ratio_limit,
            global_enabled=global_ratio_enabled,
        )
        time_limit_seconds = _effective_time_limit_seconds(
            torrent.get("seeding_time_limit", USE_GLOBAL_LIMIT),
            global_time_limit,
            global_enabled=global_time_enabled,
        )
        if ratio_limit is None and time_limit_seconds is None:
            continue

        ratio_progress = ratio / ratio_limit if ratio_limit else 0.0
        time_progress = (
            seeding_seconds / time_limit_seconds if time_limit_seconds else 0.0
        )
        weighted_ratio_progress = ratio_progress * ratio_progress_weight
        ratio_is_closer = weighted_ratio_progress >= time_progress
        closest_limit = "ratio" if ratio_is_closer else "time"
        ranked.append(
            TorrentCandidate(
                hash=torrent_hash,
                name=str(torrent.get("name", "Unknown torrent")),
                size=max(
                    _as_int(torrent.get("size", torrent.get("total_size", 0))),
                    0,
                ),
                ratio=ratio,
                ratio_limit=ratio_limit,
                upload_speed=max(_as_int(torrent.get("upspeed")), 0),
                seeding_seconds=seeding_seconds,
                time_limit_seconds=time_limit_seconds,
                closeness=ratio_progress if ratio_is_closer else time_progress,
                deletion_score=max(weighted_ratio_progress, time_progress),
                closest_limit=closest_limit,
            ),
        )

    return sorted(
        ranked,
        key=lambda item: (item.deletion_score, item.seeding_seconds, item.ratio),
        reverse=True,
    )


class QbittorrentStorageCleanup(Hass):
    """Prompt for one safe qBittorrent deletion when scratch storage is full."""

    def initialize(self) -> None:
        """Register storage and notification-action listeners."""
        self.storage_entity = str(self.args["storage_entity"])
        self.threshold = float(self.args.get("threshold", 99.9))
        self.reset_below = float(self.args.get("reset_below", self.threshold))
        self.post_delete_check_delay = max(
            float(self.args.get("post_delete_check_delay", 90)),
            1.0,
        )
        self.notify_script = str(self.args.get("notify_script", "script.notify_will"))
        self.notification_id = str(
            self.args.get("notification_id", "qbt_storage_cleanup"),
        )
        self.notification_url = str(self.args.get("notification_url", ""))
        self.client = QbittorrentWebApi(
            str(self.args["qbittorrent_url"]),
            str(self.args["qbittorrent_username"]),
            str(self.args["qbittorrent_password"]),
            ratio_progress_weight=max(
                float(self.args.get("ratio_progress_weight", 1.0)),
                0.0,
            ),
            timeout=float(self.args.get("request_timeout", 15)),
        )
        self._threshold_active = False
        self._post_delete_check_pending = False

        self.listen_state(self._storage_changed, self.storage_entity)
        self.listen_event(
            self._notification_action,
            "mobile_app_notification_action",
        )
        self.run_in(self._startup_check, 1)

    def _startup_check(self, _kwargs: dict[str, Any]) -> None:
        """Offer cleanup after reload when storage is already over the threshold."""
        usage = self._usage(self.get_state(self.storage_entity))
        if usage is not None and usage >= self.threshold:
            self._threshold_active = True
            self._offer_cleanup(usage)

    def _storage_changed(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        """Offer cleanup when storage crosses the configured threshold."""
        del entity, attribute, kwargs
        old_usage = self._usage(old)
        new_usage = self._usage(new)
        if new_usage is None:
            return

        if self._post_delete_check_pending:
            return

        if new_usage < self.reset_below:
            if self._threshold_active:
                self._clear_notification()
            self._threshold_active = False
            return

        crossed_threshold = new_usage >= self.threshold and (
            old_usage is None or old_usage < self.threshold
        )
        if crossed_threshold and not self._threshold_active:
            self._threshold_active = True
            self._offer_cleanup(new_usage)

    def _offer_cleanup(self, usage: float) -> None:
        """Find the nearest-limit torrent and send a confirmation notification."""
        try:
            ranked = self.client.ranked_seeders()
        except QbittorrentError as error:
            self.error("Unable to rank qBittorrent seeders: %s", error)
            self._notify(
                title="qBittorrent cleanup unavailable",
                message=f"Storage is at {usage:.1f}%, but qBittorrent could not be queried.",
                icon="mdi:harddisk-alert",
            )
            return

        if not ranked:
            self._notify(
                title="qBittorrent storage full",
                message=(
                    f"Storage is at {usage:.1f}%, but there are no seeding torrents "
                    "with an active ratio or time limit."
                ),
                icon="mdi:harddisk-alert",
            )
            return

        candidate = next((item for item in ranked if not item.is_uploading), None)
        if candidate is None:
            self._notify(
                title="qBittorrent storage full",
                message=(
                    f"Storage is at {usage:.1f}%, but every eligible seeding torrent "
                    "is currently uploading. Nothing will be offered for deletion."
                ),
                icon="mdi:upload-network",
            )
            return

        self._notify(
            title="Delete qBittorrent torrent?",
            message=(
                f"Storage is at {usage:.1f}%. Delete the seeding torrent closest "
                "to automatic removal?\n\n"
                f"{candidate.name}\n"
                f"Size: {candidate.size_formatted}\n"
                f"Ratio: {candidate.ratio:.2f}"
                + (
                    f" / {candidate.ratio_limit:g}"
                    if candidate.ratio_limit is not None
                    else ""
                )
                + f"\nProgress: {candidate.limit_summary}"
            ),
            icon="mdi:harddisk-remove",
            actions=[
                {
                    "action": f"{ACTION_PREFIX}{candidate.hash}",
                    "title": "Delete torrent",
                },
            ],
        )
        self.log(
            "Offered deletion of %s (%s, ratio %.2f, %.1f%% towards %s limit)",
            candidate.name,
            candidate.size_formatted,
            candidate.ratio,
            candidate.closeness * 100,
            candidate.closest_limit,
        )

    def _notification_action(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Delete the exact seeding torrent confirmed in the notification."""
        del event_type, kwargs
        action = data.get("action")
        if not isinstance(action, str) or not action.startswith(ACTION_PREFIX):
            return

        torrent_hash = action.removeprefix(ACTION_PREFIX).lower()
        if not TORRENT_HASH_PATTERN.fullmatch(torrent_hash):
            self.error("Ignored malformed qBittorrent deletion action")
            return

        try:
            candidate = next(
                (
                    item
                    for item in self.client.ranked_seeders()
                    if item.hash == torrent_hash
                ),
                None,
            )
        except QbittorrentError as error:
            self.error("Unable to delete confirmed qBittorrent torrent: %s", error)
            self._notify(
                title="Torrent was not deleted",
                message=f"qBittorrent reported: {error}",
                icon="mdi:delete-alert",
            )
            return

        if candidate is None:
            error = "the confirmed torrent is no longer in the seeding list"
            self.error("Unable to delete confirmed qBittorrent torrent: %s", error)
            self._notify(
                title="Torrent was not deleted",
                message=f"qBittorrent reported: {error}",
                icon="mdi:delete-alert",
            )
            return

        if candidate.is_uploading:
            self.log(
                "Refused deletion of %s because it is now uploading at %i B/s",
                candidate.name,
                candidate.upload_speed,
            )
            self._notify(
                title="Torrent was not deleted",
                message=(
                    f"{candidate.name} started uploading after the notification "
                    "was sent, so it has been left alone."
                ),
                icon="mdi:upload-network",
            )
            return

        try:
            self.client.delete_with_files(torrent_hash)
        except QbittorrentError as error:
            self.error("Unable to delete confirmed qBittorrent torrent: %s", error)
            self._notify(
                title="Torrent was not deleted",
                message=f"qBittorrent reported: {error}",
                icon="mdi:delete-alert",
            )
            return

        self._notify(
            title="Torrent deleted",
            message=f"Deleted {candidate.name} and its {candidate.size_formatted} of content.",
            icon="mdi:delete-check",
            persistent=False,
            sticky=False,
        )
        self.log("Deleted confirmed torrent %s (%s)", candidate.name, torrent_hash)
        self._threshold_active = False
        self._post_delete_check_pending = True
        self.run_in(self._post_delete_check, self.post_delete_check_delay)

    def _post_delete_check(self, _kwargs: dict[str, Any]) -> None:
        """Offer another deletion if storage remains full after sensor refresh."""
        self._post_delete_check_pending = False
        usage = self._usage(self.get_state(self.storage_entity))
        if usage is None or usage < self.threshold:
            return

        self._threshold_active = True
        self.log(
            "Storage remains at %.1f%% after deletion; offering another torrent",
            usage,
        )
        self._offer_cleanup(usage)

    def _notify(
        self,
        *,
        title: str,
        message: str,
        icon: str,
        actions: list[dict[str, Any]] | None = None,
        persistent: bool = True,
        sticky: bool = True,
    ) -> None:
        """Send a notification through the shared Will notification script."""
        variables: dict[str, Any] = {
            "title": title,
            "message": message,
            "notification_id": self.notification_id,
            "mobile_notification_icon": icon,
            "sticky": sticky,
            "persistent": persistent,
            # Keep this as a native list: script.notify_will forwards it directly
            # to the companion app's `data.actions` notification field.
            "actions": actions or [],
        }
        if self.notification_url:
            variables["url"] = self.notification_url
        self.call_service(
            "script/turn_on",
            entity_id=self.notify_script,
            variables=variables,
        )

    def _clear_notification(self) -> None:
        """Clear the cleanup prompt once storage has fallen sufficiently."""
        self.call_service(
            "script/turn_on",
            entity_id=self.notify_script,
            variables={
                "clear_notification": True,
                "notification_id": self.notification_id,
                "message": "clear_notification",
            },
        )

    @staticmethod
    def _usage(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
