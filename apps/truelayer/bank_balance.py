"""Get bank account/card balances from TrueLayer."""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from json import dumps
from pathlib import Path
from random import choice
from string import ascii_letters
from typing import TYPE_CHECKING, Any, Literal
from urllib import parse

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from requests import HTTPError
from wg_utilities.clients import TrueLayerClient
from wg_utilities.clients.oauth_client import OAuthCredentials
from wg_utilities.clients.truelayer import Account, Bank, Card
from wg_utilities.loggers import add_warehouse_handler

if TYPE_CHECKING:
    from collections.abc import Callable

TrueLayerClient.HEADLESS_MODE = True


class EntityType(StrEnum):
    """The type of entity."""

    ACCOUNT = "account"
    CARD = "card"


class BankBalanceGetter(Hass):  # type: ignore[misc]
    """Get bank account/card balances from TrueLayer."""

    bank: Bank
    client: TrueLayerClient
    entities: dict[EntityType, dict[str, Account] | dict[str, Card]]
    state_token: str | None

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.bank = Bank[self.args["bank_ref"].upper().replace(" ", "_")]
        self.auth_code_input_text = (
            f"input_text.truelayer_auth_token_{self.bank.name.lower()}"
        )
        self.redirect_uri = "https://console.truelayer.com/redirect-page"
        self.notification_id = f"truelayer_access_token_{self.bank.name.lower()}_expired"

        self.client = TrueLayerClient(
            client_id=self.args["client_id"],
            client_secret=self.args["client_secret"],
            creds_cache_dir=Path("/homeassistant/.wg-utilities/oauth_credentials"),
            bank=self.bank,
        )

        self.entities = {}
        self.initialize_entities()

        self.listen_state(
            self.consume_auth_token,
            self.auth_code_input_text,
        )

    def _callback_factory(
        self,
        entity_key: EntityType,
    ) -> Callable[[dict[str, Any]], None]:
        """Return a callback to update the entity balances."""

        def update_entity_balances(_: dict[str, Any]) -> None:
            """Loop through the account/card IDs and retrieve their balances."""
            for entity_ref, entity in self.entities[entity_key].items():
                variable_id = f"var.truelayer_balance_{self.bank.name.lower()}"

                if entity_ref != "no_ref":
                    variable_id += f"_{entity_ref}"

                self.log("Updating `%s` balance", variable_id)

                self.call_service(
                    "var/set",
                    entity_id=variable_id,
                    value=entity.balance,
                    force_update=True,
                )

            self.log(
                "Updated entity balances: %s",
                ", ".join(self.entities[entity_key].keys()),
            )

        return update_entity_balances

    def initialize_entities(self) -> None:
        """Initialize the TrueLayer cards and/or accounts."""
        for entity_type in EntityType:
            self._initialize_entities(entity_type)

        self.log("Initialized: %s", dumps(self.entities, default=str))

    def _initialize_entities(
        self,
        entity_type: EntityType,
    ) -> None:
        self.entities.setdefault(entity_type, {})

        get_entity_by_id = (
            self.client.get_card_by_id
            if entity_type == EntityType.CARD
            else self.client.get_account_by_id
        )

        list_entities: Callable[[], list[Account | Card]] = (
            self.client.list_cards  # type: ignore[assignment]
            if entity_type == EntityType.CARD
            else self.client.list_accounts
        )

        for entity_ref, entity_id in self.args.get(f"{entity_type}_ids", {}).items():
            try:
                if entity_id is None:
                    if len(entities := list_entities()) == 1:
                        entity: Account | Card = entities[0]
                    else:
                        self.error(
                            "Multiple %s found for `%s`, please specify an ID",
                            entity_type.title(),
                            entity_ref,
                        )
                        continue
                elif (entity := get_entity_by_id(entity_id)) is None:  # type: ignore[assignment]
                    self.error(
                        "%s not found for `%s` with ID `%s`",
                        entity_type.title(),
                        entity_ref,
                        entity_id,
                    )
                    continue
            except HTTPError as err:
                if not (
                    err.response.url == self.client.ACCESS_TOKEN_ENDPOINT
                    and err.response.status_code == HTTPStatus.BAD_REQUEST
                ):
                    self.error(
                        "Error response (%s %s) from %s: %s",
                        err.response.status_code,
                        err.response.reason,
                        err.response.url,
                        err.response.text,
                    )
                    raise

                try:
                    self.run_first_time_login()
                except Exception as login_err:
                    raise login_err from err
                else:
                    return

            self.entities[entity_type][entity_ref] = entity  # type: ignore[assignment]

        if self.entities[entity_type]:
            callback = self._callback_factory(entity_type)

            self.run_every(callback, "now", 15 * 60)
            self.log(
                "Added callback for %s balances: %s",
                entity_type,
                ", ".join(self.entities[entity_type].keys()),
            )

        self.clear_notifications()

    def run_first_time_login(self) -> None:
        """Run the first time login process."""
        self.log("Running first time login")

        self.state_token = "".join(choice(ascii_letters) for _ in range(32))  # noqa: S311

        auth_link_params = {
            "client_id": self.client.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": self.state_token,
            "access_type": "offline",
            "prompt": "consent",
            "scope": " ".join(self.client.scopes),
        }

        auth_link = self.client.auth_link_base + "?" + parse.urlencode(auth_link_params)

        self.log(auth_link)

        self.call_service(
            "script/turn_on",
            entity_id="script.notify_will",
            variables={
                "clear_notification": True,
                "message": f"TrueLayer access token for {self.bank} has expired!",
                "notification_id": f"truelayer_access_token_{self.bank.name.lower()}_expired",
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

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Override the error method to prepend the bank name."""
        super().error(f"{self.bank} | {msg}", *args, **kwargs)

    def log(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Override the log method to prepend the bank name."""
        super().log(f"{self.bank} | {msg}", *args, **kwargs)

    def refresh_access_token(self, _: dict[str, Any]) -> None:
        """Refresh the access token."""
        self.log("Refreshing access token", self.bank)

        self.client.refresh_access_token()
        self.log("Refreshed access token")

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

        self.log("Consuming auth code %s", new)

        try:
            credentials: dict[str, Any] = self.client.post_json_response(  # type: ignore[assignment]
                self.client.access_token_endpoint,
                json={
                    "code": new,
                    "grant_type": "authorization_code",
                    "client_id": self.client.client_id,
                    "client_secret": self.client.client_secret,
                    "redirect_uri": self.redirect_uri,
                },
                header_overrides={},
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

        self.initialize_entities()

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
