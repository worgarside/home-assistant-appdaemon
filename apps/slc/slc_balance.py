"""Poll the SLC portal and publish grouped Home Assistant MQTT sensors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from json import dumps
from typing import Any, Final

import appdaemon.plugins.hass.hassapi as hass
import httpx2
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from slc_client import LoanSummary, SlcError, fetch_loan_summary

DEFAULT_POLL_INTERVAL: Final[int] = 6 * 60 * 60
DEFAULT_FAILURE_THRESHOLD: Final[int] = 2
DEFAULT_NOTIFY_SCRIPT: Final[str] = "script.notify_will"
DEFAULT_NOTIFICATION_ID: Final[str] = "slc_balance_poll_failed"
MAX_NOTIFICATION_ERROR_CHARS: Final[int] = 240


@dataclass(frozen=True)
class SensorSpec:
    """MQTT discovery metadata for one SLC sensor.

    Attributes:
        key: Short sensor key used in MQTT state topics.
        object_id: Home Assistant MQTT ``object_id`` / entity object id.
        unique_id: Stable Home Assistant unique id.
        name: Friendly sensor name shown in Home Assistant.
        unit_of_measurement: Optional unit published in discovery.
        device_class: Optional Home Assistant device class.
        state_class: Optional Home Assistant state class.
        entity_category: Optional Home Assistant entity category.
    """

    key: str
    object_id: str
    unique_id: str
    name: str
    unit_of_measurement: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    entity_category: str | None = None


SENSORS: Final[tuple[SensorSpec, ...]] = (
    SensorSpec(
        key="balance",
        object_id="slc_balance",
        unique_id="appdaemon_slc_balance",
        name="Balance",
        unit_of_measurement="GBP",
        device_class="monetary",
        state_class="total",
    ),
    SensorSpec(
        key="interest_rate",
        object_id="slc_interest_rate",
        unique_id="appdaemon_slc_interest_rate",
        name="Interest rate",
        unit_of_measurement="%",
        state_class="measurement",
    ),
    SensorSpec(
        key="as_of_date",
        object_id="slc_as_of_date",
        unique_id="appdaemon_slc_as_of_date",
        name="As of date",
        device_class="date",
    ),
    SensorSpec(
        key="current_year",
        object_id="slc_current_year",
        unique_id="appdaemon_slc_current_year",
        name="Current year",
    ),
    SensorSpec(
        key="salary_repayments",
        object_id="slc_salary_repayments",
        unique_id="appdaemon_slc_salary_repayments",
        name="Salary repayments",
        unit_of_measurement="GBP",
        device_class="monetary",
        state_class="total",
    ),
    SensorSpec(
        key="direct_repayments",
        object_id="slc_direct_repayments",
        unique_id="appdaemon_slc_direct_repayments",
        name="Direct repayments",
        unit_of_measurement="GBP",
        device_class="monetary",
        state_class="total",
    ),
    SensorSpec(
        key="interest_added",
        object_id="slc_interest_added",
        unique_id="appdaemon_slc_interest_added",
        name="Interest added",
        unit_of_measurement="GBP",
        device_class="monetary",
        state_class="total",
    ),
    SensorSpec(
        key="last_successful_scrape",
        object_id="slc_last_successful_scrape",
        unique_id="appdaemon_slc_last_successful_scrape",
        name="Last successful scrape",
        device_class="timestamp",
        entity_category="diagnostic",
    ),
)


class SlcBalance(hass.Hass):
    """Publish Student Loans Company overview values as MQTT sensors.

    The app authenticates to the SLC portal on a schedule, publishes retained
    MQTT discovery/state for eight sensors under one device, and notifies via
    ``script.notify_will`` after repeated poll failures.
    """

    mqtt_client: Any | None
    notification_id: str
    notify_script: str
    raw_sensor: str | None
    username: str
    password: str
    secret_answer: str

    def initialize(self) -> None:
        """Initialize credentials, MQTT discovery, and the poll schedule.

        Required ``apps.yaml`` args are ``username``, ``password``, and
        ``secret_answer``. Optional args configure poll interval, MQTT topics,
        failure threshold, and notification targets.
        """
        self.mqtt_client = None
        self._consecutive_failures = 0
        self._mqtt_reported_available = False
        self._failure_notified = False
        self._last_failure_message: str | None = None

        for required in ("username", "password", "secret_answer"):
            if required not in self.args:
                self.error("%s is required in apps.yaml", required)
                return

        self.username = str(self.args["username"])
        self.password = str(self.args["password"])
        self.secret_answer = str(self.args["secret_answer"])
        self.poll_interval = int(self.args.get("poll_interval", DEFAULT_POLL_INTERVAL))
        self.availability_failure_threshold = max(
            1,
            int(
                self.args.get(
                    "availability_failure_threshold",
                    DEFAULT_FAILURE_THRESHOLD,
                ),
            ),
        )
        self.raw_sensor = self.args.get("raw_sensor")
        self.notify_script = str(self.args.get("notify_script", DEFAULT_NOTIFY_SCRIPT))
        self.notification_id = str(
            self.args.get("notification_id", DEFAULT_NOTIFICATION_ID),
        )

        self._configure_mqtt()
        self.run_every(self.poll_slc, "now", self.poll_interval)
        self.log(
            "Initialized SLC balance polling every %s seconds "
            "(availability offline after %s consecutive failures)",
            self.poll_interval,
            self.availability_failure_threshold,
        )

    def terminate(self) -> None:
        """Mark sensors offline and disconnect the MQTT client."""
        if self.mqtt_client is None:
            return

        self._publish_mqtt_availability(is_available=False)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    def poll_slc(self, kwargs: dict[str, Any] | None = None) -> None:
        """Fetch the SLC overview and publish MQTT sensor states.

        On success, sensor states and availability are updated and any prior
        failure notification is cleared. On failure, diagnostics are updated
        and consecutive-failure availability/notification logic runs.

        Args:
            kwargs: Unused AppDaemon scheduler kwargs.
        """
        del kwargs

        try:
            summary = fetch_loan_summary(
                username=self.username,
                password=self.password,
                secret_answer=self.secret_answer,
            )
        except (SlcError, httpx2.HTTPError) as err:
            self.error("SLC poll failed: %s", err)
            self._write_raw_sensor("error", error=str(err))
            self._set_availability(is_available=False, error=str(err))
            return

        self._publish_sensor_states(summary, scraped_at=datetime.now(UTC))
        self._write_raw_sensor("ok", summary=summary)
        self._set_availability(is_available=True)
        self._clear_failure_notification()
        self.log(
            "Published SLC overview (balance present=%s, year=%s, as_of=%s)",
            summary.balance is not None,
            summary.current_year or "n/a",
            summary.as_of_date.isoformat() if summary.as_of_date else "n/a",
        )

    def _configure_mqtt(self) -> None:
        """Configure and connect the MQTT client used for discovery and state.

        If ``mqtt_host`` is unset, MQTT publishing is disabled and the app
        continues without discovery.
        """
        self.mqtt_host = self.args.get("mqtt_host")
        self.mqtt_port = int(self.args.get("mqtt_port", 1883))
        self.mqtt_username = self.args.get("mqtt_username")
        self.mqtt_password = self.args.get("mqtt_password")
        self.mqtt_discovery_prefix = str(
            self.args.get("mqtt_discovery_prefix", "homeassistant"),
        ).strip("/")
        self.mqtt_device_id = str(
            self.args.get("mqtt_device_id", "appdaemon_slc_student_loan"),
        )
        self.mqtt_device_name = str(self.args.get("mqtt_device_name", "Student Loan"))
        self.mqtt_base_topic = str(
            self.args.get("mqtt_base_topic", "appdaemon/slc"),
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
        except Exception as err:
            self.error(
                "Failed to connect to MQTT broker %s:%s: %s",
                self.mqtt_host,
                self.mqtt_port,
                err,
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
        """Publish discovery and force availability online after MQTT connect.

        Args:
            client: Connected Paho MQTT client.
            userdata: Unused client userdata.
            flags: Unused connect flags.
            reason_code: MQTT connect reason/result code.
            properties: Unused MQTT v5 properties.
        """
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

        self.log("MQTT connected for %s", self.mqtt_device_id)
        self._publish_mqtt_discovery()
        self._set_availability(is_available=True, force=True)

    def _mqtt_topic(self, topic: str) -> str:
        """Build an absolute MQTT topic under the configured base topic.

        Args:
            topic: Relative topic suffix, for example ``availability``.

        Returns:
            Absolute topic path under ``mqtt_base_topic``.
        """
        return f"{self.mqtt_base_topic}/{topic}"

    def _publish_mqtt(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        """Publish a retained or non-retained MQTT payload.

        Non-string payloads are JSON-encoded with sorted keys.

        Args:
            topic: Absolute MQTT topic.
            payload: String or JSON-serializable payload.
            retain: Whether the broker should retain the message.
        """
        if self.mqtt_client is None:
            return

        if not isinstance(payload, str):
            payload = dumps(payload, sort_keys=True)

        self.mqtt_client.publish(topic, payload, qos=self.mqtt_qos, retain=retain)

    def _device_block(self) -> dict[str, Any]:
        """Build the shared Home Assistant MQTT device registry block.

        Returns:
            Device metadata used by all SLC sensor discovery payloads.
        """
        return {
            "identifiers": [self.mqtt_device_id],
            "manufacturer": "Student Loans Company",
            "model": "Manage Balance Portal",
            "name": self.mqtt_device_name,
        }

    def _origin_block(self) -> dict[str, str]:
        """Build the MQTT discovery origin metadata block.

        Returns:
            Origin metadata identifying this AppDaemon app.
        """
        return {
            "name": "AppDaemon SLC Balance",
            "sw": "home-assistant-appdaemon",
        }

    def _publish_mqtt_discovery(self) -> None:
        """Publish Home Assistant MQTT discovery configs for all SLC sensors."""
        availability_topic = self._mqtt_topic("availability")
        for sensor in SENSORS:
            config: dict[str, Any] = {
                "name": sensor.name,
                "unique_id": sensor.unique_id,
                "object_id": sensor.object_id,
                "state_topic": self._mqtt_topic(f"{sensor.key}/state"),
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
            if sensor.entity_category is not None:
                config["entity_category"] = sensor.entity_category

            config_topic = (
                f"{self.mqtt_discovery_prefix}/sensor/{sensor.object_id}/config"
            )
            self._publish_mqtt(config_topic, config)

    def _publish_mqtt_availability(self, *, is_available: bool) -> None:
        """Publish the shared MQTT availability payload.

        Args:
            is_available: Whether sensors should be marked online.
        """
        self._publish_mqtt(
            self._mqtt_topic("availability"),
            "online" if is_available else "offline",
        )

    def _value_for_sensor(
        self,
        summary: LoanSummary,
        key: str,
        *,
        scraped_at: datetime,
    ) -> str | None:
        """Format a loan-summary field for MQTT state publication.

        Args:
            summary: Parsed SLC overview values.
            key: Sensor key from ``SensorSpec.key``.
            scraped_at: UTC datetime of the successful scrape.

        Returns:
            String state payload, or None if the value is missing/unknown.
        """
        values: dict[str, str | None] = {
            "balance": None if summary.balance is None else f"{summary.balance:.2f}",
            "interest_rate": (
                None
                if summary.interest_rate_pct is None
                else f"{summary.interest_rate_pct:g}"
            ),
            "as_of_date": (
                None if summary.as_of_date is None else summary.as_of_date.isoformat()
            ),
            "current_year": summary.current_year,
            "salary_repayments": (
                None
                if summary.salary_repayments is None
                else f"{summary.salary_repayments:.2f}"
            ),
            "direct_repayments": (
                None
                if summary.direct_repayments is None
                else f"{summary.direct_repayments:.2f}"
            ),
            "interest_added": (
                None
                if summary.interest_added is None
                else f"{summary.interest_added:.2f}"
            ),
            "last_successful_scrape": scraped_at.isoformat(),
        }
        return values.get(key)

    def _publish_sensor_states(
        self,
        summary: LoanSummary,
        *,
        scraped_at: datetime,
    ) -> None:
        """Publish retained MQTT state topics for all known sensor values.

        Args:
            summary: Parsed SLC overview values.
            scraped_at: UTC datetime of the successful scrape.
        """
        if self.mqtt_client is None:
            return

        for sensor in SENSORS:
            value = self._value_for_sensor(summary, sensor.key, scraped_at=scraped_at)
            if value is None:
                continue
            self._publish_mqtt(self._mqtt_topic(f"{sensor.key}/state"), value)

    def _write_raw_sensor(
        self,
        state: str,
        *,
        summary: LoanSummary | None = None,
        error: str | None = None,
    ) -> None:
        """Update the optional diagnostic ``raw_sensor`` entity.

        Args:
            state: Entity state string, typically ``ok`` or ``error``.
            summary: Successful overview values to store as attributes.
            error: Failure message to store as an attribute.
        """
        if self.raw_sensor is None:
            return

        attributes: dict[str, Any] = {
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if summary is not None:
            attributes["current_year"] = summary.current_year
            attributes["balance"] = summary.balance
            attributes["interest_rate_pct"] = summary.interest_rate_pct
            attributes["as_of_date"] = (
                summary.as_of_date.isoformat() if summary.as_of_date else None
            )
            attributes["salary_repayments"] = summary.salary_repayments
            attributes["direct_repayments"] = summary.direct_repayments
            attributes["interest_added"] = summary.interest_added
        if error:
            attributes["error"] = error

        self.set_state(self.raw_sensor, state=state, attributes=attributes)

    def _set_availability(
        self,
        *,
        is_available: bool,
        force: bool = False,
        error: str | None = None,
    ) -> None:
        """Update MQTT availability based on consecutive poll outcomes.

        Successful polls reset the failure counter. Failures only mark sensors
        offline and trigger a notification after the configured threshold.

        Args:
            is_available: Whether the latest poll succeeded.
            force: Republish online even if already marked available.
            error: Optional failure reason retained for notification text.
        """
        if is_available:
            self._consecutive_failures = 0
            self._last_failure_message = None
            if self._mqtt_reported_available and not force:
                return

            self._mqtt_reported_available = True
            self._publish_mqtt_availability(is_available=True)
            return

        self._consecutive_failures += 1
        if error:
            self._last_failure_message = error

        if self._consecutive_failures < self.availability_failure_threshold:
            self.log(
                "Transient SLC failure (%s/%s); keeping sensors available",
                self._consecutive_failures,
                self.availability_failure_threshold,
            )
            return

        if self._mqtt_reported_available:
            self._mqtt_reported_available = False
            self._publish_mqtt_availability(is_available=False)

        self._notify_failure()

    def _notify_failure(self) -> None:
        """Send a one-shot failure notification via the configured script."""
        if self._failure_notified:
            return

        message = self._last_failure_message or (
            "Student loan balance could not be retrieved."
        )
        # Keep notification copy short and free of credentials / tokens.
        if len(message) > MAX_NOTIFICATION_ERROR_CHARS:
            keep = MAX_NOTIFICATION_ERROR_CHARS - 3
            message = f"{message[:keep]}..."

        self.call_service(
            "script/turn_on",
            entity_id=self.notify_script,
            variables={
                "clear_notification": True,
                "title": "Student Loan Balance Update Failed",
                "message": (
                    "Could not refresh SLC sensors. "
                    f"The portal login/page layout may have changed. ({message})"
                ),
                "notification_id": self.notification_id,
                "mobile_notification_icon": "mdi:school-outline",
            },
        )
        self._failure_notified = True
        self.log("Sent SLC failure notification (%s)", self.notification_id)

    def _clear_failure_notification(self) -> None:
        """Clear the sticky failure notification after a successful poll."""
        if not self._failure_notified:
            return

        self.call_service(
            "script/turn_on",
            entity_id=self.notify_script,
            variables={
                "clear_notification": True,
                "notification_id": self.notification_id,
            },
        )
        self._failure_notified = False
        self.log("Cleared SLC failure notification (%s)", self.notification_id)
