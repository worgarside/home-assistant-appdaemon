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


@dataclass(frozen=True)
class SensorSpec:
    """MQTT discovery metadata for one SLC sensor."""

    key: str
    object_id: str
    unique_id: str
    name: str
    unit_of_measurement: str | None = None
    device_class: str | None = None
    state_class: str | None = None


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
)


class SlcBalance(hass.Hass):
    """Publish Student Loans Company overview values as MQTT sensors."""

    mqtt_client: Any | None
    raw_sensor: str | None
    username: str
    password: str
    secret_answer: str

    def initialize(self) -> None:
        """Initialize the app."""
        self.mqtt_client = None
        self._consecutive_failures = 0
        self._mqtt_reported_available = False

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

        self._configure_mqtt()
        self.run_every(self.poll_slc, "now", self.poll_interval)
        self.log(
            "Initialized SLC balance polling every %s seconds "
            "(availability offline after %s consecutive failures)",
            self.poll_interval,
            self.availability_failure_threshold,
        )

    def terminate(self) -> None:
        """Cleanly disconnect the MQTT client on AppDaemon shutdown."""
        if self.mqtt_client is None:
            return

        self._publish_mqtt_availability(is_available=False)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    def poll_slc(self, kwargs: dict[str, Any] | None = None) -> None:
        """Fetch the SLC overview and publish MQTT sensor states."""
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
            self._set_availability(is_available=False)
            return

        self._publish_sensor_states(summary)
        self._write_raw_sensor("ok", summary=summary)
        self._set_availability(is_available=True)
        self.log(
            "Published SLC overview (balance present=%s, year=%s)",
            summary.balance is not None,
            summary.current_year or "n/a",
        )

    def _configure_mqtt(self) -> None:
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
        return f"{self.mqtt_base_topic}/{topic}"

    def _publish_mqtt(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        if self.mqtt_client is None:
            return

        if not isinstance(payload, str):
            payload = dumps(payload, sort_keys=True)

        self.mqtt_client.publish(topic, payload, qos=self.mqtt_qos, retain=retain)

    def _device_block(self) -> dict[str, Any]:
        return {
            "identifiers": [self.mqtt_device_id],
            "manufacturer": "Student Loans Company",
            "model": "Manage Balance Portal",
            "name": self.mqtt_device_name,
        }

    def _origin_block(self) -> dict[str, str]:
        return {
            "name": "AppDaemon SLC Balance",
            "sw": "home-assistant-appdaemon",
        }

    def _publish_mqtt_discovery(self) -> None:
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

            config_topic = (
                f"{self.mqtt_discovery_prefix}/sensor/{sensor.object_id}/config"
            )
            self._publish_mqtt(config_topic, config)

    def _publish_mqtt_availability(self, *, is_available: bool) -> None:
        self._publish_mqtt(
            self._mqtt_topic("availability"),
            "online" if is_available else "offline",
        )

    def _value_for_sensor(self, summary: LoanSummary, key: str) -> str | None:
        values: dict[str, str | None] = {
            "balance": None if summary.balance is None else f"{summary.balance:.2f}",
            "interest_rate": (
                None
                if summary.interest_rate_pct is None
                else f"{summary.interest_rate_pct:g}"
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
        }
        return values.get(key)

    def _publish_sensor_states(self, summary: LoanSummary) -> None:
        if self.mqtt_client is None:
            return

        for sensor in SENSORS:
            value = self._value_for_sensor(summary, sensor.key)
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
        if self.raw_sensor is None:
            return

        attributes: dict[str, Any] = {
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if summary is not None:
            attributes["current_year"] = summary.current_year
            attributes["balance"] = summary.balance
            attributes["interest_rate_pct"] = summary.interest_rate_pct
            attributes["salary_repayments"] = summary.salary_repayments
            attributes["direct_repayments"] = summary.direct_repayments
            attributes["interest_added"] = summary.interest_added
        if error:
            attributes["error"] = error

        self.set_state(self.raw_sensor, state=state, attributes=attributes)

    def _set_availability(self, *, is_available: bool, force: bool = False) -> None:
        if is_available:
            self._consecutive_failures = 0
            if self._mqtt_reported_available and not force:
                return

            self._mqtt_reported_available = True
            self._publish_mqtt_availability(is_available=True)
            return

        self._consecutive_failures += 1

        if self._consecutive_failures < self.availability_failure_threshold:
            self.log(
                "Transient SLC failure (%s/%s); keeping sensors available",
                self._consecutive_failures,
                self.availability_failure_threshold,
            )
            return

        if not self._mqtt_reported_available:
            return

        self._mqtt_reported_available = False
        self._publish_mqtt_availability(is_available=False)
