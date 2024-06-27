"""Manage my Monzo pot for credit card payments."""

from __future__ import annotations

from datetime import UTC, datetime
from json import JSONDecodeError, dumps
from pathlib import Path
from time import sleep
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib import parse

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-untyped]
from requests import HTTPError
from wg_utilities.clients import MonzoClient
from wg_utilities.clients.oauth_client import OAuthCredentials
from wg_utilities.loggers import add_warehouse_handler

if TYPE_CHECKING:
    from wg_utilities.clients.monzo import Pot


class CreditCardPotManager(Hass):  # type: ignore[misc]
    """Keep my credit card pot topped up with nightly notifications."""

    ACTION_PHRASE: Final = "TOP_UP_CREDIT_CARD_POT"
    NOTIFICATION_ICON: Final = "mdi:credit-card-plus-outline"

    client: MonzoClient
    credit_card_pot: Pot

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.notification_id = "monzo_credit_card_pot_access_token_expired"
        self.auth_code_input_text = "input_text.monzo_auth_token_cc_pot_top_up"
        self.redirect_uri = "https://console.truelayer.com/redirect-page"

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

        self.run_daily(self.run_daily_process, "21:00:00")

    def run_daily_process(self, _: dict[str, Any] | None = None) -> None:
        """Top up (or send a notification to) the credit card pot."""
        amex_balance = float(self.get_state("var.truelayer_balance_amex"))
        monzo_credit_cards_balance = float(
            self.get_state("var.truelayer_balance_monzo_credit_cards"),
        )

        if (deficit := round(amex_balance - monzo_credit_cards_balance, 2)) <= 0.0:
            if abs(deficit) < 50:  # noqa: PLR2004
                # Filter out notifications for the day after payments, when the deficit is -£PAYMENT
                self.call_service(
                    "script/turn_on",
                    entity_id="script.notify_will",
                    variables={
                        "clear_notification": True,
                        "title": "Credit Card Pot top up skipped",
                        "message": "No top up needed!",
                        "notification_id": self.ACTION_PHRASE,
                        "mobile_notification_icon": self.NOTIFICATION_ICON.replace(
                            "plus",
                            "check",
                        ),
                    },
                )
            return

        monzo_current_account_balance = float(
            self.get_state("var.truelayer_balance_monzo_current_account"),
        )

        min_remainder = float(
            self.get_state("input_number.credit_card_pot_top_up_minimum_remainder"),
        )

        max_auto_top_up = float(
            self.get_state("input_number.credit_card_pot_top_up_maximum_auto_top_up"),
        )

        available = max(monzo_current_account_balance - min_remainder, 0)
        top_up_amount = min(available, deficit)

        notification_action = {
            "action": f"{self.ACTION_PHRASE}:{top_up_amount}",
            "title": f"Top Up (£{top_up_amount:.2f})",
        }

        if top_up_amount < max_auto_top_up:
            self.log(
                "Credit Cards pot is £%.2f too low. Topping up by £%.2f",
                deficit,
                top_up_amount,
            )
            self.top_up_credit_card_pot(
                "mobile_app_notification_action",
                notification_action,
                {},
            )

            message = (
                f"£{top_up_amount:.2f} has been added to the credit card pot. "
                f"Remaining balance: £{(monzo_current_account_balance - top_up_amount):.2f}"
            )

            self.log(message)

            self.call_service(
                "script/turn_on",
                entity_id="script.notify_will",
                variables={
                    "clear_notification": True,
                    "title": "Credit Card Pot automatically topped up",
                    "message": message,
                    "notification_id": self.ACTION_PHRASE,
                    "mobile_notification_icon": self.NOTIFICATION_ICON,
                    "actions": dumps(
                        [
                            {
                                "action": "URI",
                                "title": "Open Monzo",
                                "uri": "app://co.uk.getmondo",
                            },
                        ],
                    ),
                },
            )
        else:
            message = (
                f"Credit Cards pot is £{deficit:.2f} too low. Top up pot?\n\n"
                f"Amount remaining: £{(monzo_current_account_balance - top_up_amount):.2f}"
            )
            data = {
                "actions": [notification_action],
                "tag": self.ACTION_PHRASE,
                "notification_icon": self.NOTIFICATION_ICON,
                "visibility": "private",
            }

            self.notify(
                name="mobile_app_will_s_pixel_6_pro",
                message=message,
                title="Top up Credit Card Pot?",
                data=data,
            )

            self.log(
                "Sent notification to top up credit card pot by £%.2f",
                top_up_amount,
            )

    def top_up_credit_card_pot(
        self,
        _: Literal["mobile_app_notification_action"],
        data: dict[str, Any],
        __: dict[str, str],
    ) -> None:
        """Top up the credit card pot when a notification action is received."""
        action_phrase, top_up_amount_str = data.get("action", "0:0").split(":")

        if action_phrase != self.ACTION_PHRASE:
            return

        top_up_amount = round(float(top_up_amount_str) * 100)

        if not 0 < top_up_amount < 10000 * 100:
            self.error("Invalid top up amount %s", top_up_amount)
            return

        self.client.deposit_into_pot(
            self.credit_card_pot,
            amount_pence=top_up_amount,
            dedupe_id=datetime.now(UTC).strftime(f"{self.name}-%Y%m%d"),
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
            "redirect_uri": self.redirect_uri,
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
            if self.initialize_entities(send_notification=False):
                break
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
