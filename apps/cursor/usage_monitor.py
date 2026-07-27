"""Track Cursor subscription usage through its dashboard API."""

from __future__ import annotations

import base64
import json
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
DEFAULT_TOKEN_PATH: Final[Path] = Path(
    "/homeassistant/.wg-utilities/cursor/session_token",
)
HTTP_TIMEOUT_SECONDS: Final[int] = 30
REMOVED_SENSOR_OBJECT_IDS: Final[tuple[str, ...]] = ()


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

    @property
    def unique_id(self) -> str:
        """Return the stable Home Assistant unique ID."""
        return f"appdaemon_{self.object_id}"


SENSORS: Final[tuple[SensorSpec, ...]] = (
    SensorSpec(
        key="included_usage_used",
        object_id="cursor_included_usage_used",
        name="Included usage used",
        unit_of_measurement="USD",
        device_class="monetary",
        state_class="total",
    ),
    SensorSpec(
        key="included_usage_limit",
        object_id="cursor_included_usage_limit",
        name="Included usage limit",
        unit_of_measurement="USD",
        device_class="monetary",
        state_class="total",
    ),
    SensorSpec(
        key="included_usage_remaining",
        object_id="cursor_included_usage_remaining",
        name="Included usage remaining",
        unit_of_measurement="USD",
        device_class="monetary",
        state_class="total",
    ),
    SensorSpec(
        key="included_usage_percent",
        object_id="cursor_included_usage_percent",
        name="Included usage",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:percent-circle-outline",
    ),
    SensorSpec(
        key="auto_percent_used",
        object_id="cursor_auto_percent_used",
        name="Auto usage",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:robot-outline",
    ),
    SensorSpec(
        key="api_percent_used",
        object_id="cursor_api_percent_used",
        name="API usage",
        unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:api",
    ),
    SensorSpec(
        key="total_percent_used",
        object_id="cursor_total_percent_used",
        name="Total usage",
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
        state_class="total",
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
)


class CursorUsageMonitor(hass.Hass):
    """Poll Cursor usage and publish grouped MQTT discovery sensors."""

    mqtt_client: Any | None

    def initialize(self) -> None:
        """Initialize token handling, MQTT, and the poll schedule."""
        self.poll_interval = int(
            self.args.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL),
        )
        self.token_path = Path(self.args.get("token_path", DEFAULT_TOKEN_PATH))
        self.http = Session()
        self._latest_state: dict[str, Any] = {}
        self.mqtt_client = None

        self._configure_mqtt()
        self.listen_event(self.receive_session_token, "cursor_session_token")
        self.run_every(self.poll_cursor, "now", self.poll_interval)
        self.log("Initialized Cursor usage polling every %s seconds", self.poll_interval)

    def terminate(self) -> None:
        """Mark sensors offline and close network clients."""
        self.http.close()
        if self.mqtt_client is None:
            return
        self._publish_mqtt_availability(is_available=False)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

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
            self._publish_error_state("unauthenticated", "Token event was empty")
            return

        try:
            self._token_expiry(token)
            self._persist_token(token)
        except (OSError, CursorAuthenticationError, ValueError) as error:
            self.error("Cursor session token was rejected: %s", error)
            self._publish_error_state("unauthenticated", str(error))
            return

        self.log("Stored a refreshed Cursor session token")
        self.run_in(self.poll_cursor, 0)

    def poll_cursor(self, kwargs: dict[str, Any] | None = None) -> None:
        """Fetch Cursor usage and publish the latest state."""
        del kwargs
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            self._publish_error_state(
                "unauthenticated",
                "Waiting for a Cursor session token",
            )
            return
        except OSError as error:
            self.error("Unable to read Cursor session token: %s", error)
            self._publish_error_state("error", str(error))
            return

        try:
            state = self._fetch_usage(token)
        except CursorAuthenticationError as error:
            self.error("Cursor authentication failed: %s", error)
            self._publish_error_state("unauthenticated", str(error), token=token)
            return
        except (RequestException, TypeError, ValueError, KeyError) as error:
            self.error("Cursor usage poll failed: %s", error)
            self._publish_error_state("error", str(error), token=token)
            return

        self._latest_state.update(state)
        self._publish_state()
        self.log(
            "Published Cursor usage: %.2f%% of included usage",
            state["included_usage_percent"],
        )

    def _persist_token(self, token: str) -> None:
        """Atomically persist the token with owner-only permissions."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path = self.token_path.with_suffix(".tmp")
        temporary_path.write_text(token, encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(self.token_path)
        self.token_path.chmod(0o600)

    @staticmethod
    def _token_claims(cookie_value: str) -> dict[str, Any]:
        """Decode and validate the JWT claims in Cursor's session cookie."""
        decoded_cookie = unquote(cookie_value)
        try:
            jwt = decoded_cookie.split("::", maxsplit=1)[1]
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
        now = datetime.now(UTC).isoformat()
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
            "usage_status": "ok",
            "usage_status_attributes": {
                "last_updated": now,
                "error": None,
                "membership_type": summary.get("membershipType"),
                "hard_limit_enabled": bool(hard_limit.get("noUsageBasedAllowed")),
                "display_message": current_period.get("displayMessage"),
            },
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
        status: str,
        error: str,
        *,
        token: str | None = None,
    ) -> None:
        """Publish a diagnostic status while retaining last good usage values."""
        self._latest_state["usage_status"] = status
        self._latest_state["usage_status_attributes"] = {
            "last_updated": datetime.now(UTC).isoformat(),
            "error": error,
        }
        if token is not None:
            with suppress(CursorAuthenticationError):
                self._latest_state["session_token_expiry"] = self._token_expiry(
                    token,
                ).isoformat()
        self._publish_state()

    def _configure_mqtt(self) -> None:
        """Configure and connect the MQTT client."""
        self.mqtt_host = self.args.get("mqtt_host")
        self.mqtt_port = int(self.args.get("mqtt_port", 1883))
        self.mqtt_username = self.args.get("mqtt_username")
        self.mqtt_password = self.args.get("mqtt_password")
        self.mqtt_discovery_prefix = str(
            self.args.get("mqtt_discovery_prefix", "homeassistant"),
        ).strip("/")
        self.mqtt_device_id = str(
            self.args.get("mqtt_device_id", "appdaemon_cursor_usage"),
        )
        self.mqtt_device_name = str(self.args.get("mqtt_device_name", "Cursor"))
        self.mqtt_base_topic = str(
            self.args.get("mqtt_base_topic", "appdaemon/cursor_usage"),
        ).strip("/")
        self.mqtt_qos = int(self.args.get("mqtt_qos", 0))

        if not self.mqtt_host:
            self.log("No mqtt_host configured; MQTT sensor discovery disabled")
            return

        client_id = str(
            self.args.get("mqtt_client_id", f"appdaemon-{self.mqtt_device_id}"),
        )
        self.mqtt_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        if self.mqtt_username:
            self.mqtt_client.username_pw_set(self.mqtt_username, self.mqtt_password)

        self.mqtt_client.on_connect = self._handle_mqtt_connect
        self.mqtt_client.will_set(
            self._mqtt_topic("availability"),
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
            "Configured MQTT sensor discovery for %s at %s",
            self.mqtt_device_id,
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
            self.error(
                "MQTT connection failed for %s: %s",
                self.mqtt_device_id,
                reason_code,
            )
            return

        self._publish_mqtt_discovery()
        self._remove_old_discovery_configs()
        self._publish_mqtt_availability(is_available=True)
        self._publish_state()
        self.log("MQTT connected for %s", self.mqtt_device_id)

    def _mqtt_topic(self, suffix: str) -> str:
        """Build a topic under the configured MQTT base topic."""
        return f"{self.mqtt_base_topic}/{suffix}"

    def _publish_mqtt(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        """Publish a retained or transient MQTT payload."""
        if self.mqtt_client is None:
            return
        if not isinstance(payload, str):
            payload = json.dumps(payload, sort_keys=True)
        self.mqtt_client.publish(topic, payload, qos=self.mqtt_qos, retain=retain)

    def _publish_mqtt_discovery(self) -> None:
        """Publish Home Assistant discovery configs for all Cursor sensors."""
        state_topic = self._mqtt_topic("state")
        availability_topic = self._mqtt_topic("availability")
        for sensor in SENSORS:
            config: dict[str, Any] = {
                "name": sensor.name,
                "unique_id": sensor.unique_id,
                "object_id": sensor.object_id,
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{sensor.key} }}}}",
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": self._device_block(),
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

            config_topic = (
                f"{self.mqtt_discovery_prefix}/sensor/{sensor.object_id}/config"
            )
            self._publish_mqtt(config_topic, config)

    def _remove_old_discovery_configs(self) -> None:
        """Remove retained discovery configs for explicitly retired sensors."""
        for object_id in REMOVED_SENSOR_OBJECT_IDS:
            config_topic = f"{self.mqtt_discovery_prefix}/sensor/{object_id}/config"
            self._publish_mqtt(config_topic, "")

    def _device_block(self) -> dict[str, Any]:
        """Return shared Home Assistant device metadata."""
        return {
            "identifiers": [self.mqtt_device_id],
            "manufacturer": "Cursor",
            "model": "Ultra",
            "name": self.mqtt_device_name,
        }

    @staticmethod
    def _origin_block() -> dict[str, str]:
        """Return MQTT discovery origin metadata."""
        return {
            "name": "AppDaemon Cursor Usage",
            "sw": "home-assistant-appdaemon",
        }

    def _publish_mqtt_availability(self, *, is_available: bool) -> None:
        """Publish shared MQTT availability."""
        self._publish_mqtt(
            self._mqtt_topic("availability"),
            "online" if is_available else "offline",
        )

    def _publish_state(self) -> None:
        """Publish the latest combined state payload."""
        if self._latest_state:
            self._publish_mqtt(self._mqtt_topic("state"), self._latest_state)
