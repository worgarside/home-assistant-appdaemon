"""Track Cursor subscription usage through its dashboard API."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import unquote

import appdaemon.plugins.hass.hassapi as hass
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from requests import HTTPError, RequestException, Response, Session

CURSOR_API_BASE: Final[str] = "https://cursor.com"
DEFAULT_POLL_INTERVAL: Final[int] = 5 * 60
DEFAULT_TOKEN_PATH: Final[Path] = Path("/data/cursor/session_token")
DEFAULT_ACCOUNT_STORE_PATH: Final[Path] = Path("/data/cursor/accounts")
HTTP_TIMEOUT_SECONDS: Final[int] = 30
ACCOUNT_STORE_VERSION: Final[int] = 1
PAYLOAD_VERSION: Final[int] = 1
ACCOUNT_ID_LENGTH: Final[int] = 12
ACCOUNT_SLUG_HASH_LENGTH: Final[int] = 6


class CursorAuthenticationError(RuntimeError):
    """Raised when Cursor rejects or cannot use the session token."""


@dataclass(frozen=True)
class SensorSpec:
    """MQTT discovery metadata for one Cursor sensor."""

    key: str
    object_id: str
    name: str
    unit_of_measurement: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    attributes_key: str | None = None

    def account_object_id(self, account_slug: str) -> str:
        """Return the account-scoped Home Assistant object ID."""
        metric = self.object_id.removeprefix("cursor_")
        return f"cursor_{account_slug}_{metric}"

    def unique_id(self, account_slug: str) -> str:
        """Return the account-scoped Home Assistant unique ID."""
        return f"appdaemon_{self.account_object_id(account_slug)}"


@dataclass(frozen=True)
class CursorAccount:
    """Persisted identity and token for one Cursor account."""

    account_id: str
    account_slug: str
    subject: str
    email: str
    display_name: str | None
    team_id: int | None
    team_name: str | None
    token: str
    is_legacy: bool = False

    @property
    def device_id(self) -> str:
        """Return the stable MQTT device identifier."""
        return f"appdaemon_cursor_usage_{self.account_id}"

    @property
    def device_name(self) -> str:
        """Return a human-readable Home Assistant device name."""
        label = self.display_name or self.email
        if self.team_name:
            return f"Cursor — {label} ({self.team_name})"
        return f"Cursor — {label}"


SENSORS: Final[tuple[SensorSpec, ...]] = (
    SensorSpec(
        key="included_usage_used",
        object_id="cursor_included_usage_used",
        name="Included usage used",
        unit_of_measurement="USD",
        device_class="monetary",
        state_class="total_increasing",
    ),
    SensorSpec(
        key="included_usage_limit",
        object_id="cursor_included_usage_limit",
        name="Included usage limit",
        unit_of_measurement="USD",
        device_class="monetary",
        state_class="measurement",
    ),
    SensorSpec(
        key="included_usage_remaining",
        object_id="cursor_included_usage_remaining",
        name="Included usage remaining",
        unit_of_measurement="USD",
        device_class="monetary",
        state_class="measurement",
    ),
    SensorSpec(
        key="included_usage_percent",
        object_id="cursor_included_usage_percent",
        name="Included usage percent",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:percent-circle-outline",
    ),
    SensorSpec(
        key="auto_percent_used",
        object_id="cursor_auto_percent_used",
        name="Auto usage percent",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:robot-outline",
    ),
    SensorSpec(
        key="api_percent_used",
        object_id="cursor_api_percent_used",
        name="API usage percent",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:api",
    ),
    SensorSpec(
        key="total_percent_used",
        object_id="cursor_total_percent_used",
        name="Total usage percent",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:gauge",
    ),
    SensorSpec(
        key="billing_cycle_start",
        object_id="cursor_billing_cycle_start",
        name="Billing cycle start",
        device_class="timestamp",
        icon="mdi:calendar-start",
    ),
    SensorSpec(
        key="billing_cycle_end",
        object_id="cursor_billing_cycle_end",
        name="Billing cycle end",
        device_class="timestamp",
        icon="mdi:calendar-end",
    ),
    SensorSpec(
        key="usage_by_model",
        object_id="cursor_usage_by_model",
        name="Usage by model",
        unit_of_measurement="¢",
        state_class="total_increasing",
        icon="mdi:chart-donut",
        attributes_key="usage_by_model_attributes",
    ),
    SensorSpec(
        key="session_token_expiry",
        object_id="cursor_session_token_expiry",
        name="Session token expiry",
        device_class="timestamp",
        icon="mdi:clock-alert-outline",
        entity_category="diagnostic",
    ),
    SensorSpec(
        key="usage_status",
        object_id="cursor_usage_status",
        name="Usage status",
        icon="mdi:cloud-check-outline",
        entity_category="diagnostic",
        attributes_key="usage_status_attributes",
    ),
    SensorSpec(
        key="cycle_percent_elapsed",
        object_id="cursor_cycle_percent_elapsed",
        name="Cycle percent elapsed",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:progress-clock",
    ),
    SensorSpec(
        key="cycle_days_remaining",
        object_id="cursor_cycle_days_remaining",
        name="Cycle days remaining",
        unit_of_measurement="d",
        device_class="duration",
        state_class="measurement",
        icon="mdi:calendar-clock",
    ),
    SensorSpec(
        key="usage_pace",
        object_id="cursor_usage_pace",
        name="Usage pace",
        state_class="measurement",
        icon="mdi:speedometer",
    ),
    SensorSpec(
        key="projected_cycle_usage",
        object_id="cursor_projected_cycle_usage",
        name="Projected cycle usage",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:chart-timeline-variant-shimmer",
    ),
)

LEGACY_MQTT_SENSOR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "included_usage_used",
        "included_usage_limit",
        "included_usage_remaining",
        "included_usage_percent",
        "auto_percent_used",
        "api_percent_used",
        "total_percent_used",
        "billing_cycle_start",
        "billing_cycle_end",
        "usage_by_model",
        "session_token_expiry",
        "usage_status",
    },
)
DERIVED_SENSOR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "cycle_percent_elapsed",
        "cycle_days_remaining",
        "usage_pace",
        "projected_cycle_usage",
    },
)


class CursorUsageMonitor(hass.Hass):
    """Poll Cursor usage per account and publish MQTT discovery sensors."""

    mqtt_client: Any | None

    def initialize(self) -> None:
        """Initialize account storage, MQTT, token handling, and polling."""
        self.poll_interval = int(
            self.args.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL),
        )
        self.legacy_token_path = Path(
            self.args.get("token_path", DEFAULT_TOKEN_PATH),
        )
        self.account_store_path = Path(
            self.args.get("account_store_path", DEFAULT_ACCOUNT_STORE_PATH),
        )
        self.legacy_account_email = str(
            self.args.get("legacy_account_email", ""),
        ).strip()
        if not self.legacy_account_email:
            msg = "legacy_account_email is required during Cursor account migration"
            raise ValueError(msg)
        self.publish_legacy_derived_sensors = bool(
            self.args.get("publish_legacy_derived_sensors", False),
        )
        self.http = Session()
        self.accounts: dict[str, CursorAccount] = {}
        self._latest_states: dict[str, dict[str, Any]] = {}
        self._device_models: dict[str, str] = {}
        self._mqtt_connected = False
        self.mqtt_client = None

        self._load_accounts()
        self._migrate_legacy_token()
        self._configure_mqtt()
        self.listen_event(self.receive_session_token, "cursor_session_token")
        self.run_every(self.poll_cursor, "immediate", self.poll_interval)
        self.log(
            "Initialized Cursor usage polling for %s account(s) every %s seconds",
            len(self.accounts),
            self.poll_interval,
        )

    def terminate(self) -> None:
        """Mark sensors offline and close network clients."""
        self.http.close()
        if self.mqtt_client is None:
            return
        self._publish_mqtt(self._mqtt_global_topic("availability"), "offline")
        for account in self.accounts.values():
            self._publish_account_availability(account, is_available=False)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    @staticmethod
    def _account_id(subject: str) -> str:
        """Return a stable, non-identifying account key."""
        return hashlib.sha256(subject.encode()).hexdigest()[:ACCOUNT_ID_LENGTH]

    @staticmethod
    def _account_slug(email: str, account_id: str) -> str:
        """Return a readable, collision-resistant entity ID segment."""
        email_slug = re.sub(r"[^a-z0-9]+", "_", email.casefold()).strip("_")
        return f"{email_slug}_{account_id[:ACCOUNT_SLUG_HASH_LENGTH]}"

    def _account_file(self, account_id: str) -> Path:
        """Return the persisted account record path."""
        return self.account_store_path / f"{account_id}.json"

    def _persist_account(self, account: CursorAccount) -> None:
        """Atomically persist one account record with owner-only permissions."""
        self.account_store_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.account_store_path.chmod(0o700)
        account_path = self._account_file(account.account_id)
        temporary_path = account_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "version": ACCOUNT_STORE_VERSION,
                    "account_id": account.account_id,
                    "account_slug": account.account_slug,
                    "subject": account.subject,
                    "email": account.email,
                    "display_name": account.display_name,
                    "team_id": account.team_id,
                    "team_name": account.team_name,
                    "token": account.token,
                    "is_legacy": account.is_legacy,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(account_path)
        account_path.chmod(0o600)

    @staticmethod
    def _deserialize_account(payload: Any) -> CursorAccount:
        """Validate and deserialize one persisted account record."""
        if not isinstance(payload, dict):
            raise TypeError("Cursor account record is not an object")
        if payload.get("version") != ACCOUNT_STORE_VERSION:
            raise ValueError("Cursor account record has an unsupported version")
        required_strings = (
            "account_id",
            "account_slug",
            "subject",
            "email",
            "token",
        )
        for key in required_strings:
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise ValueError(f"Cursor account record has no {key}")
        display_name = payload.get("display_name")
        team_id = payload.get("team_id")
        team_name = payload.get("team_name")
        return CursorAccount(
            account_id=payload["account_id"],
            account_slug=payload["account_slug"],
            subject=payload["subject"],
            email=payload["email"],
            display_name=display_name if isinstance(display_name, str) else None,
            team_id=team_id
            if isinstance(team_id, int) and not isinstance(team_id, bool)
            else None,
            team_name=team_name if isinstance(team_name, str) else None,
            token=payload["token"],
            is_legacy=payload.get("is_legacy") is True,
        )

    @staticmethod
    def _validate_persisted_subject(
        account: CursorAccount,
        claims: dict[str, Any],
    ) -> None:
        """Require a persisted account and token to identify the same subject."""
        if claims.get("sub") != account.subject:
            raise CursorAuthenticationError(
                "Persisted account subject does not match its token",
            )

    def _validate_legacy_owner(self, account: CursorAccount) -> None:
        """Require the migrated account to match the configured legacy owner."""
        if account.email != self.legacy_account_email:
            raise CursorAuthenticationError(
                "Stored legacy token conflicts with its configured owner",
            )

    def _load_accounts(self) -> None:
        """Load all valid persisted account records."""
        if not self.account_store_path.exists():
            return
        for account_path in self.account_store_path.glob("*.json"):
            try:
                payload = json.loads(account_path.read_text(encoding="utf-8"))
                account = self._deserialize_account(payload)
                claims = self._token_claims(account.token)
                self._validate_persisted_subject(account, claims)
            except (
                CursorAuthenticationError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                self.error(
                    "Ignoring invalid Cursor account file %s: %s",
                    account_path,
                    error,
                )
                continue
            self.accounts[account.account_id] = account

    def _migrate_legacy_token(self) -> None:
        """Import the existing single-account token without changing ownership."""
        try:
            token = self.legacy_token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return
        except OSError as error:
            self.error("Unable to read legacy Cursor token: %s", error)
            return
        try:
            claims = self._token_claims(token)
            subject = str(claims["sub"])
            account_id = self._account_id(subject)
            existing = self.accounts.get(account_id)
            if existing is not None:
                self._validate_legacy_owner(existing)
                return
            account = CursorAccount(
                account_id=account_id,
                account_slug=self._account_slug(self.legacy_account_email, account_id),
                subject=subject,
                email=self.legacy_account_email,
                display_name=None,
                team_id=None,
                team_name=None,
                token=token,
                is_legacy=True,
            )
            self._persist_account(account)
        except (CursorAuthenticationError, OSError, ValueError) as error:
            self.error("Unable to migrate legacy Cursor token: %s", error)
            return
        self.accounts[account.account_id] = account
        self.log("Imported legacy Cursor token for %s", account.email)

    def _account_from_event(
        self,
        token: str,
        data: dict[str, Any],
    ) -> CursorAccount:
        """Validate an event and return the account record it represents."""
        claims = self._token_claims(token)
        subject = str(claims["sub"])
        account_id = self._account_id(subject)
        existing = self.accounts.get(account_id)
        metadata = data.get("account")
        if metadata is None:
            if existing is None:
                raise CursorAuthenticationError(
                    "Account metadata is required for a new Cursor account",
                )
            return CursorAccount(
                account_id=existing.account_id,
                account_slug=existing.account_slug,
                subject=existing.subject,
                email=existing.email,
                display_name=existing.display_name,
                team_id=existing.team_id,
                team_name=existing.team_name,
                token=token,
                is_legacy=existing.is_legacy,
            )
        if data.get("version") != PAYLOAD_VERSION or not isinstance(metadata, dict):
            raise CursorAuthenticationError("Cursor token event has an invalid payload")
        if metadata.get("subject") != subject:
            raise CursorAuthenticationError(
                "Cursor account subject does not match its token",
            )
        email = metadata.get("email")
        if not isinstance(email, str) or not email.strip():
            raise CursorAuthenticationError("Cursor account email is missing")
        email = email.strip()
        is_legacy = existing.is_legacy if existing is not None else False
        if is_legacy and email.casefold() != self.legacy_account_email.casefold():
            raise CursorAuthenticationError(
                "Legacy Cursor account email does not match its configured owner",
            )
        display_name = metadata.get("display_name")
        team_id = metadata.get("team_id")
        team_name = metadata.get("team_name")
        return CursorAccount(
            account_id=account_id,
            account_slug=(
                existing.account_slug
                if existing is not None
                else self._account_slug(email, account_id)
            ),
            subject=subject,
            email=email,
            display_name=(
                display_name.strip()
                if isinstance(display_name, str) and display_name.strip()
                else None
            ),
            team_id=(
                team_id
                if isinstance(team_id, int) and not isinstance(team_id, bool)
                else None
            ),
            team_name=(
                team_name.strip()
                if isinstance(team_name, str) and team_name.strip()
                else None
            ),
            token=token,
            is_legacy=is_legacy,
        )

    def receive_session_token(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Validate and persist a session token received from Home Assistant."""
        del event_type, kwargs
        token = data.get("token")
        if not isinstance(token, str) or not token:
            self.error("Cursor session token event did not contain a token")
            return
        try:
            account = self._account_from_event(token, data)
            self._token_expiry(token)
            self._persist_account(account)
        except (OSError, CursorAuthenticationError, TypeError, ValueError) as error:
            self.error("Cursor session token was rejected: %s", error)
            return

        self.accounts[account.account_id] = account
        self._device_models.setdefault(account.account_id, "Cursor")
        if self._mqtt_connected:
            self._publish_mqtt_discovery(account)
            self._publish_account_availability(account, is_available=True)
        self.log("Stored a refreshed Cursor session token for %s", account.email)
        self.run_in(self.poll_account, 0, account_id=account.account_id)

    def poll_cursor(self, kwargs: dict[str, Any] | None = None) -> None:
        """Fetch and publish usage for every stored Cursor account."""
        del kwargs
        if not self.accounts:
            self.log("Waiting for a Cursor session token")
            return
        for account in tuple(self.accounts.values()):
            self._poll_account(account)

    def poll_account(self, kwargs: dict[str, Any]) -> None:
        """Fetch and publish one account selected by an AppDaemon callback."""
        account_id = kwargs.get("account_id")
        if not isinstance(account_id, str):
            self.error("Cursor account poll was scheduled without an account ID")
            return
        account = self.accounts.get(account_id)
        if account is not None:
            self._poll_account(account)

    def _poll_account(self, account: CursorAccount) -> None:
        """Fetch Cursor usage and publish one account's latest state."""
        try:
            state = self._fetch_usage(account.token)
        except CursorAuthenticationError as error:
            self.error("Cursor authentication failed for %s: %s", account.email, error)
            self._publish_error_state(
                account,
                "unauthenticated",
                str(error),
                token=account.token,
            )
            return
        except (RequestException, TypeError, ValueError, KeyError) as error:
            self.error("Cursor usage poll failed for %s: %s", account.email, error)
            self._publish_error_state(account, "error", str(error), token=account.token)
            return

        latest_state = self._latest_states.setdefault(account.account_id, {})
        latest_state.update(state)
        attributes = state.get("usage_status_attributes")
        membership_type = (
            attributes.get("membership_type") if isinstance(attributes, dict) else None
        )
        self._maybe_update_device_model(account, membership_type)
        self._publish_state(account)
        self.log(
            "Published Cursor usage for %s: %.2f%% of included usage",
            account.email,
            state["included_usage_percent"],
        )

    @staticmethod
    def _token_claims(cookie_value: str) -> dict[str, Any]:
        """Decode and validate the JWT claims in Cursor's session cookie."""
        decoded_cookie = unquote(cookie_value)
        try:
            cookie_subject, jwt = decoded_cookie.split("::", maxsplit=1)
            encoded_payload = jwt.split(".")[1]
        except IndexError as error:
            raise CursorAuthenticationError(
                "Cursor session cookie has an unexpected format",
            ) from error

        encoded_payload += "=" * (-len(encoded_payload) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(encoded_payload))
        except (json.JSONDecodeError, ValueError) as error:
            raise CursorAuthenticationError("Cursor session JWT is invalid") from error

        if not isinstance(claims, dict):
            raise CursorAuthenticationError("Cursor session JWT payload is invalid")
        if claims.get("type") != "session" or claims.get("aud") != CURSOR_API_BASE:
            raise CursorAuthenticationError("Cursor session JWT claims are invalid")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise CursorAuthenticationError("Cursor session JWT has no subject")
        if cookie_subject != subject.rsplit("|", maxsplit=1)[-1]:
            raise CursorAuthenticationError("Cursor session cookie subject is invalid")
        return cast("dict[str, Any]", claims)

    @classmethod
    def _token_expiry(cls, cookie_value: str) -> datetime:
        """Return the validated UTC token expiry."""
        expires_at = cls._token_claims(cookie_value).get("exp")
        if not isinstance(expires_at, int):
            raise CursorAuthenticationError("Cursor session JWT has no expiry")
        expiry = datetime.fromtimestamp(expires_at, tz=UTC)
        if expiry <= datetime.now(UTC):
            raise CursorAuthenticationError("Cursor session token has expired")
        return expiry

    def _request_json(
        self,
        method: str,
        path: str,
        token: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call one Cursor dashboard endpoint and return a JSON object."""
        headers = {
            "Cookie": f"WorkosCursorSessionToken={token}",
            "Origin": CURSOR_API_BASE,
        }
        response: Response
        try:
            response = self.http.request(
                method,
                f"{CURSOR_API_BASE}{path}",
                headers=headers,
                json=body,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except HTTPError as error:
            if error.response is not None and error.response.status_code in {401, 403}:
                raise CursorAuthenticationError(
                    f"Cursor returned HTTP {error.response.status_code}",
                ) from error
            raise

        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError(f"Cursor returned a non-object response for {path}")
        return cast("dict[str, Any]", payload)

    @staticmethod
    def _timestamp_milliseconds(value: str) -> str:
        """Convert an ISO timestamp to Unix milliseconds."""
        parsed = datetime.fromisoformat(value)
        return str(int(parsed.timestamp() * 1000))

    def _fetch_usage(self, token: str) -> dict[str, Any]:
        """Fetch and normalize current-cycle usage from Cursor."""
        token_expiry = self._token_expiry(token)
        summary = self._request_json("GET", "/api/usage-summary", token)
        current_period = self._request_json(
            "POST",
            "/api/dashboard/get-current-period-usage",
            token,
            body={},
        )
        hard_limit = self._request_json(
            "POST",
            "/api/dashboard/get-hard-limit",
            token,
            body={},
        )

        cycle_start = str(summary["billingCycleStart"])
        cycle_end = str(summary["billingCycleEnd"])
        aggregate_body = {
            "startDate": self._timestamp_milliseconds(cycle_start),
            "endDate": self._timestamp_milliseconds(cycle_end),
        }
        aggregated = self._request_json(
            "POST",
            "/api/dashboard/get-aggregated-usage-events",
            token,
            body=aggregate_body,
        )

        individual_usage = summary["individualUsage"]
        if not isinstance(individual_usage, dict):
            raise TypeError("Cursor individualUsage is not an object")
        plan = individual_usage["plan"]
        if not isinstance(plan, dict):
            raise TypeError("Cursor plan usage is not an object")

        used_cents = float(plan["used"])
        limit_cents = float(plan["limit"])
        remaining_cents = float(plan["remaining"])
        included_percent = 0.0
        if limit_cents > 0:
            included_percent = used_cents / limit_cents * 100

        models, model_total_cents = self._normalize_models(
            aggregated.get("aggregations"),
        )
        now = datetime.now(UTC)
        cycle_metrics = self._cycle_metrics(cycle_start, cycle_end, now)
        usage_pace = 0.0
        projected_usage = 0.0
        if cycle_metrics["cycle_percent_elapsed"] > 0:
            usage_pace = included_percent / cycle_metrics["cycle_percent_elapsed"]
            projected_usage = usage_pace * 100
        return {
            "included_usage_used": round(used_cents / 100, 2),
            "included_usage_limit": round(limit_cents / 100, 2),
            "included_usage_remaining": round(remaining_cents / 100, 2),
            "included_usage_percent": round(included_percent, 2),
            "auto_percent_used": round(float(plan["autoPercentUsed"]), 2),
            "api_percent_used": round(float(plan["apiPercentUsed"]), 2),
            "total_percent_used": round(float(plan["totalPercentUsed"]), 2),
            "billing_cycle_start": cycle_start,
            "billing_cycle_end": cycle_end,
            "usage_by_model": round(model_total_cents, 2),
            "usage_by_model_attributes": {"models": models},
            "session_token_expiry": token_expiry.isoformat(),
            **cycle_metrics,
            "usage_pace": round(usage_pace, 3),
            "projected_cycle_usage": round(projected_usage, 2),
            "usage_status": "ok",
            "usage_status_attributes": {
                "last_updated": now.isoformat(),
                "error": None,
                "membership_type": summary.get("membershipType"),
                "hard_limit_enabled": bool(hard_limit.get("noUsageBasedAllowed")),
                "display_message": current_period.get("displayMessage"),
            },
        }

    @staticmethod
    def _cycle_metrics(
        cycle_start: str,
        cycle_end: str,
        now: datetime,
    ) -> dict[str, float]:
        """Calculate billing-cycle progress metrics."""
        start = datetime.fromisoformat(cycle_start)
        end = datetime.fromisoformat(cycle_end)
        duration_seconds = (end - start).total_seconds()
        if duration_seconds <= 0:
            return {"cycle_percent_elapsed": 0.0, "cycle_days_remaining": 0.0}
        elapsed_seconds = (now - start).total_seconds()
        percent_elapsed = min(100.0, max(0.0, elapsed_seconds / duration_seconds * 100))
        days_remaining = max(0.0, (end - now).total_seconds() / 86400)
        return {
            "cycle_percent_elapsed": round(percent_elapsed, 2),
            "cycle_days_remaining": round(days_remaining, 2),
        }

    @staticmethod
    def _normalize_models(value: Any) -> tuple[dict[str, Any], float]:
        """Normalize Cursor's per-model aggregate list for HA attributes."""
        if not isinstance(value, list):
            raise TypeError("Cursor model aggregations are not a list")

        models: dict[str, Any] = {}
        total_cents = 0.0
        for item in value:
            if not isinstance(item, dict):
                continue
            model = str(item.get("modelIntent", "unknown"))
            model_cents = float(item.get("totalCents", 0))
            total_cents += model_cents
            models[model] = {
                "input_tokens": int(item.get("inputTokens", 0)),
                "output_tokens": int(item.get("outputTokens", 0)),
                "cache_read_tokens": int(item.get("cacheReadTokens", 0)),
                "cache_write_tokens": int(item.get("cacheWriteTokens", 0)),
                "total_cents": round(model_cents, 2),
            }
        return models, total_cents

    def _publish_error_state(
        self,
        account: CursorAccount,
        status: str,
        error: str,
        *,
        token: str | None = None,
    ) -> None:
        """Publish a diagnostic status while retaining last good usage values."""
        latest_state = self._latest_states.setdefault(account.account_id, {})
        latest_state["usage_status"] = status
        latest_state["usage_status_attributes"] = {
            "last_updated": datetime.now(UTC).isoformat(),
            "error": error,
        }
        if token is not None:
            with suppress(CursorAuthenticationError):
                latest_state["session_token_expiry"] = self._token_expiry(
                    token,
                ).isoformat()
        self._publish_state(account)

    def _configure_mqtt(self) -> None:
        """Configure and connect the MQTT client."""
        self.mqtt_host = self.args.get("mqtt_host")
        self.mqtt_port = int(self.args.get("mqtt_port", 1883))
        self.mqtt_username = self.args.get("mqtt_username")
        self.mqtt_password = self.args.get("mqtt_password")
        self.mqtt_discovery_prefix = str(
            self.args.get("mqtt_discovery_prefix", "homeassistant"),
        ).strip("/")
        self.mqtt_base_topic = str(
            self.args.get("mqtt_base_topic", "appdaemon/cursor_usage"),
        ).strip("/")
        self.mqtt_qos = int(self.args.get("mqtt_qos", 0))

        if not self.mqtt_host:
            self.log("No mqtt_host configured; MQTT sensor discovery disabled")
            return

        client_id = str(
            self.args.get("mqtt_client_id", "appdaemon-cursor-usage"),
        )
        self.mqtt_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        if self.mqtt_username:
            self.mqtt_client.username_pw_set(self.mqtt_username, self.mqtt_password)

        self.mqtt_client.on_connect = self._handle_mqtt_connect
        self.mqtt_client.will_set(
            self._mqtt_global_topic("availability"),
            "offline",
            qos=self.mqtt_qos,
            retain=True,
        )
        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        except Exception as error:
            self.error(
                "Failed to connect to MQTT broker %s:%s: %s",
                self.mqtt_host,
                self.mqtt_port,
                error,
            )
            self.mqtt_client = None
            return

        self.mqtt_client.loop_start()
        self.log(
            "Configured MQTT sensor discovery for %s account(s) at %s",
            len(self.accounts),
            self.mqtt_base_topic,
        )

    def _handle_mqtt_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Publish discovery, availability, and state after MQTT connects."""
        del client, userdata, flags, properties
        is_failure = getattr(reason_code, "is_failure", None)
        failed = bool(is_failure) if isinstance(is_failure, bool) else reason_code != 0
        if failed:
            self.error("MQTT connection failed: %s", reason_code)
            return

        self._mqtt_connected = True
        self._publish_mqtt(self._mqtt_global_topic("availability"), "online")
        for account in self.accounts.values():
            self._publish_mqtt_discovery(account)
            self._publish_account_availability(account, is_available=True)
            self._publish_state(account)
        self.log("MQTT connected for Cursor usage")

    def _mqtt_global_topic(self, suffix: str) -> str:
        """Build a shared topic under the configured MQTT base topic."""
        return f"{self.mqtt_base_topic}/{suffix}"

    def _mqtt_topic(self, account: CursorAccount, suffix: str) -> str:
        """Build an account topic under the configured MQTT base topic."""
        return f"{self.mqtt_base_topic}/{account.account_id}/{suffix}"

    def _publish_mqtt(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        """Publish a retained or transient MQTT payload."""
        if self.mqtt_client is None:
            return
        if not isinstance(payload, str):
            payload = json.dumps(payload, sort_keys=True)
        self.mqtt_client.publish(topic, payload, qos=self.mqtt_qos, retain=retain)

    def _publish_mqtt_discovery(self, account: CursorAccount) -> None:
        """Publish Home Assistant discovery configs for one account."""
        state_topic = self._mqtt_topic(account, "state")
        availability = [
            {
                "topic": self._mqtt_global_topic("availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            {
                "topic": self._mqtt_topic(account, "availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ]
        for sensor in SENSORS:
            if (
                account.is_legacy
                and sensor.key in DERIVED_SENSOR_KEYS
                and not self.publish_legacy_derived_sensors
            ):
                continue
            account_object_id = sensor.account_object_id(account.account_slug)
            legacy_discovery = account.is_legacy and sensor.key in LEGACY_MQTT_SENSOR_KEYS
            unique_id = (
                f"appdaemon_{sensor.object_id}"
                if legacy_discovery
                else sensor.unique_id(account.account_slug)
            )
            config: dict[str, Any] = {
                "name": sensor.name,
                "unique_id": unique_id,
                "object_id": account_object_id,
                "default_entity_id": f"sensor.{account_object_id}",
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{sensor.key} }}}}",
                "availability": availability,
                "availability_mode": "all",
                "device": self._device_block(account),
                "origin": self._origin_block(),
            }
            if sensor.unit_of_measurement is not None:
                config["unit_of_measurement"] = sensor.unit_of_measurement
            if sensor.device_class is not None:
                config["device_class"] = sensor.device_class
            if sensor.state_class is not None:
                config["state_class"] = sensor.state_class
            if sensor.icon is not None:
                config["icon"] = sensor.icon
            if sensor.entity_category is not None:
                config["entity_category"] = sensor.entity_category
            if sensor.attributes_key is not None:
                config["json_attributes_topic"] = state_topic
                config["json_attributes_template"] = (
                    f"{{{{ value_json.{sensor.attributes_key} | tojson }}}}"
                )

            discovery_object_id = (
                sensor.object_id if legacy_discovery else account_object_id
            )
            config_topic = (
                f"{self.mqtt_discovery_prefix}/sensor/{discovery_object_id}/config"
            )
            self._publish_mqtt(config_topic, config)

    def _maybe_update_device_model(
        self,
        account: CursorAccount,
        membership_type: Any,
    ) -> None:
        """Refresh discovery if the Cursor plan/model from the API changed."""
        if not isinstance(membership_type, str) or not membership_type.strip():
            return
        model = membership_type.replace("_", " ").strip().title()
        if model == self._device_models.get(account.account_id):
            return
        self._device_models[account.account_id] = model
        self._publish_mqtt_discovery(account)

    def _device_block(self, account: CursorAccount) -> dict[str, Any]:
        """Return Home Assistant device metadata for one account."""
        return {
            "identifiers": [account.device_id],
            "manufacturer": "Cursor",
            "model": self._device_models.get(account.account_id, "Cursor"),
            "name": account.device_name,
        }

    @staticmethod
    def _origin_block() -> dict[str, str]:
        """Return MQTT discovery origin metadata."""
        return {
            "name": "AppDaemon Cursor Usage",
            "sw": "home-assistant-appdaemon",
        }

    def _publish_account_availability(
        self,
        account: CursorAccount,
        *,
        is_available: bool,
    ) -> None:
        """Publish one account's MQTT availability."""
        self._publish_mqtt(
            self._mqtt_topic(account, "availability"),
            "online" if is_available else "offline",
        )

    def _publish_state(self, account: CursorAccount) -> None:
        """Publish one account's latest combined state payload."""
        latest_state = self._latest_states.get(account.account_id)
        if latest_state:
            self._publish_mqtt(self._mqtt_topic(account, "state"), latest_state)
