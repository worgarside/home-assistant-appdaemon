"""Manage my Monzo pot for credit card payments."""

from __future__ import annotations

from json import JSONDecodeError, dumps
from pathlib import Path
from time import sleep
from typing import TYPE_CHECKING, Any, Literal
from urllib import parse

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from requests import HTTPError
from wg_utilities.clients import MonzoClient
from wg_utilities.clients.oauth_client import OAuthCredentials
from wg_utilities.loggers import add_warehouse_handler

if TYPE_CHECKING:
    from wg_utilities.clients.monzo import Pot


class CreditCardPotManager(Hass):  # type: ignore[misc]
    """Keep my credit card pot topped up with nightly notifications."""

    client: MonzoClient
    credit_card_pot: Pot

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.notification_id = "monzo_credit_card_pot_access_token_expired"
        self.auth_code_input_text = "input_text.monzo_auth_token_cc_pot_top_up"

        self.client = MonzoClient(
            client_id=self.args["client_id"],
            client_secret=self.args["client_secret"],
            creds_cache_dir=Path("/homeassistant/.wg-utilities/oauth_credentials"),
        )

        self.initialize_entities()

        self.listen_state(
            self.consume_auth_token,
            self.auth_code_input_text,
        )

    def top_up_credit_card_pot(
        self,
        _: Literal["mobile_app_notification_action"],
        data: dict[str, Any],
        __: dict[str, str],
    ) -> None:
        """Top up the credit card pot when a notification action is received."""
        action_phrase, top_up_amount_str = data.get("action", "0:0").split(":")

        if action_phrase != "TOP_UP_CREDIT_CARD_POT":
            return

        top_up_amount = round(float(top_up_amount_str) * 100)

        if not 0 < top_up_amount < 1000 * 100:
            self.error("Invalid top up amount %s", top_up_amount)
            return

        self.client.deposit_into_pot(
            self.credit_card_pot,
            amount_pence=top_up_amount,
            dedupe_id="-".join(
                (
                    self.name,
                    str(top_up_amount),
                    data.get("metadata", {}).get("context", {}).get("id", ""),
                ),
            ),
        )

        self.log("Topped up credit card pot by %i", top_up_amount)

    def initialize_entities(self, *, send_notification: bool = True) -> bool:
        """Initialize the CC pot, or start the auth flow."""
        try:
            credit_card_pot = self.client.get_pot_by_name("credit cards")
        except HTTPError as err:
            try:
                data = err.response.json()
            except JSONDecodeError:
                self.error("Error response from Monzo: %s", err.response.text)
                raise err from None

            if data.get("code") == "forbidden.insufficient_permissions":
                if send_notification:
                    self.send_auth_link_notification()
                return False

            self.error(err.response.text)
            raise

        if not credit_card_pot:
            self.error("Could not find credit card pot")
            raise RuntimeError("Could not find credit card pot")

        self.credit_card_pot = credit_card_pot

        self.listen_event(self.top_up_credit_card_pot, "mobile_app_notification_action")

        self.log("Listen event registered for %s", self.credit_card_pot.name)

        return True

    def send_auth_link_notification(self) -> None:
        """Run the first time login process."""
        self.log("Running first time login")

        auth_link_params = {
            "client_id": self.client.client_id,
            # Reflects the code back at the user for easy copypaste
            "redirect_uri": "https://console.truelayer.com/redirect-page",
            "response_type": "code",
            "state": "abcdefghijklmnopqrstuvwxyz",
            "access_type": "offline",
            "prompt": "consent",
        }

        if self.client.scopes:
            auth_link_params["scope"] = " ".join(self.client.scopes)

        auth_link = self.client.auth_link_base + "?" + parse.urlencode(auth_link_params)

        self.log(auth_link)

        self.call_service(
            "script/turn_on",
            entity_id="script.notify_will",
            variables={
                "clear_notification": True,
                "title": "Monzo (CC top-up) Access Token Expired",
                "message": "Monzo access token has expired!",
                "notification_id": self.notification_id,
                "mobile_notification_icon": "mdi:key-alert-outline",
                "actions": dumps(
                    [
                        {"action": "URI", "title": "Auth Link", "uri": auth_link},
                        {
                            "action": "URI",
                            "title": "Submit Code",
                            "uri": f"entityId:{self.auth_code_input_text}",
                        },
                    ],
                ),
            },
        )

    def consume_auth_token(
        self,
        entity: str,
        attribute: Literal["state"],
        old: str,
        new: str,
        pin_app: bool,  # noqa: FBT001
        **kwargs: dict[str, Any],
    ) -> None:
        """Consume the auth token and get the access token."""
        _ = entity, attribute, old, pin_app, kwargs

        if not new:
            return

        if len(new) == 1:
            self.send_auth_link_notification()
            return

        self.log("Consuming auth code %s", new)

        try:
            credentials: dict[str, Any] = self.client.post_json_response(  # type: ignore[assignment]
                self.client.access_token_endpoint,
                data={
                    "code": new,
                    "grant_type": "authorization_code",
                    "client_id": self.client.client_id,
                    "client_secret": self.client.client_secret,
                    "redirect_uri": self.redirect_uri,
                },
                header_overrides={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except HTTPError as err:
            self.error(
                "Error response (%s %s) from %s: %s",
                err.response.status_code,
                err.response.reason,
                err.response.url,
                err.response.text,
            )
            return

        credentials["client_id"] = self.client.client_id
        credentials["client_secret"] = self.client.client_secret

        self.client.credentials = OAuthCredentials.parse_first_time_login(credentials)

        for _ in range(12):
            if not self.initialize_entities(send_notification=False):
                sleep(10)

        self.set_textvalue(
            entity_id=self.auth_code_input_text,
            value="",
        )

        self.clear_notifications()

    def clear_notifications(self) -> None:
        """Clear the notification."""
        self.call_service(
            "script/turn_on",
            entity_id="script.notify_will",
            variables={
                "clear_notification": True,
                "notification_id": self.notification_id,
            },
        )
