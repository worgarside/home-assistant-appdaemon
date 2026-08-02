"""Get bank account/card balances from TrueLayer."""

from __future__ import annotations

from enum import StrEnum
from json import dumps
from pathlib import Path
from re import Pattern
from re import compile as re_compile
from typing import TYPE_CHECKING, Any, Final, cast

from appdaemon.plugins.hass.hassapi import Hass
from oauth_callback_broker import (
    OAuthCallbackBroker,
    OAuthFlow,
    OAuthFlowConsumerMixin,
    OAuthFlowManager,
    OAuthProvider,
)
from requests import HTTPError
from wg_utilities.clients import TrueLayerClient
from wg_utilities.clients.truelayer import Account, Bank, Card

if TYPE_CHECKING:
    from collections.abc import Callable

TrueLayerClient.HEADLESS_MODE = True

CREDENTIALS_CACHE_DIR: Final[Path] = Path(
    "/homeassistant/.wg-utilities/oauth_credentials",
)
DEFAULT_NOTIFY_SCRIPT: Final[str] = "script.notify_will"
VARIANT_REF_PATTERN: Final[Pattern[str]] = re_compile(r"^[a-z][a-z0-9_]*$")


class EntityType(StrEnum):
    """The type of entity."""

    ACCOUNT = "account"
    CARD = "card"


class BankBalanceGetter(OAuthFlowConsumerMixin, Hass):
    """Get bank account/card balances from TrueLayer."""

    bank: Bank
    balance_slug: str
    client: TrueLayerClient
    entities: dict[EntityType, dict[str, Account] | dict[str, Card]]
    notify_script: str

    def initialize(self) -> None:
        """Initialize the app."""
        self.bank = Bank[self.args["bank_ref"].upper().replace(" ", "_")]
        self.balance_slug = self._get_balance_slug()
        self.notify_script = str(self.args.get("notify_script", DEFAULT_NOTIFY_SCRIPT))
        oauth_broker = cast(
            "OAuthCallbackBroker",
            self.get_app("oauth_callback_broker"),
        )

        client_id = self.args["client_id"]
        if credentials_cache_path := self._get_credentials_cache_path(client_id):
            self.client = TrueLayerClient(
                client_id=client_id,
                client_secret=self.args["client_secret"],
                creds_cache_path=credentials_cache_path,
                use_existing_credentials_only=True,
                bank=self.bank,
            )
        else:
            self.client = TrueLayerClient(
                client_id=client_id,
                client_secret=self.args["client_secret"],
                creds_cache_dir=CREDENTIALS_CACHE_DIR,
                use_existing_credentials_only=True,
                bank=self.bank,
            )

        self.entities = {}
        self._balance_timer_handles: dict[EntityType, str] = {}
        self.oauth = OAuthFlowManager(
            self,
            oauth_broker,
            [
                OAuthFlow(
                    ref=self.balance_slug,
                    provider=OAuthProvider.TRUELAYER,
                    client=self.client,
                    reauth_var=self.args["reauth_var"],
                    notification_id=(
                        f"truelayer_access_token_{self.balance_slug}_expired"
                    ),
                    notification_title=f"{self.bank} Access Token Expired",
                    notification_message=(
                        f"TrueLayer access token for {self.bank} has expired!"
                    ),
                    trigger_entity=self.args["reauth_trigger"],
                    notify_script=self.notify_script,
                    auth_params={"access_type": "offline", "prompt": "consent"},
                    on_authorized=self.initialize_entities,
                ),
            ],
        )
        self.initialize_entities()

    def _callback_factory(
        self,
        entity_key: EntityType,
    ) -> Callable[[dict[str, Any]], None]:
        """Return a callback to update the entity balances."""

        def update_entity_balances(_: dict[str, Any]) -> None:
            """Loop through the account/card IDs and retrieve their balances."""
            try:
                for entity_ref, entity in self.entities[entity_key].items():
                    variable_id = self._get_variable_id(entity_ref)

                    self.log("Updating `%s` balance", variable_id)

                    self.call_service(
                        "var/set",
                        entity_id=variable_id,
                        value=entity.balance,
                        force_update=True,
                    )
            except (HTTPError, RuntimeError) as err:
                if self._handle_credential_error(err):
                    return
                raise

            self.log(
                "Updated entity balances: %s",
                ", ".join(self.entities[entity_key].keys()),
            )

        return update_entity_balances

    def _get_balance_slug(self) -> str:
        """Get the slug used for HA entities and OAuth credential isolation."""
        variant_ref = self.args.get("variant_ref")

        if variant_ref is None:
            return self.bank.name.lower()

        if not isinstance(variant_ref, str) or not VARIANT_REF_PATTERN.fullmatch(
            variant_ref,
        ):
            raise ValueError(
                "`variant_ref` must be a lowercase snake_case string starting with a "
                "letter",
            )

        return variant_ref

    def _get_credentials_cache_path(self, client_id: str) -> Path | None:
        """Get an explicit credentials path for a variant, or None for defaults."""
        if self.balance_slug == self.bank.name.lower():
            return None

        credentials_cache_path = (
            CREDENTIALS_CACHE_DIR
            / TrueLayerClient.__name__
            / client_id
            / f"{self.balance_slug}.json"
        )
        credentials_cache_path.parent.mkdir(parents=True, exist_ok=True)

        return credentials_cache_path

    def _get_variable_id(self, entity_ref: str) -> str:
        """Get the HA var entity ID for a TrueLayer account/card ref."""
        variable_id = f"var.truelayer_balance_{self.balance_slug}"

        if entity_ref != "no_ref":
            variable_id += f"_{entity_ref}"

        return variable_id

    def _handle_credential_error(self, err: HTTPError | RuntimeError) -> bool:
        """Notify the appropriate user when TrueLayer needs authorisation."""
        return self.oauth.handle_authorization_error(self.balance_slug, err)

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
            except (HTTPError, RuntimeError) as err:
                if self._handle_credential_error(err):
                    return

                if isinstance(err, HTTPError):
                    self.error(
                        "Error response (%s %s) from %s: %s",
                        err.response.status_code,
                        err.response.reason,
                        err.response.url,
                        err.response.text,
                    )

                raise

            self.entities[entity_type][entity_ref] = entity  # type: ignore[assignment]

        if self.entities[entity_type]:
            if entity_type not in self._balance_timer_handles:
                callback = self._callback_factory(entity_type)
                self._balance_timer_handles[entity_type] = self.run_every(
                    callback,
                    "immediate",
                    15 * 60,
                )
                self.log(
                    "Added callback for %s balances: %s",
                    entity_type,
                    ", ".join(self.entities[entity_type].keys()),
                )

            self.oauth.clear(self.balance_slug)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Override the error method to prepend the bank name."""
        super().error(f"{self.bank} | {msg}", *args, **kwargs)

    def log(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Override the log method to prepend the bank name."""
        super().log(f"{self.bank} | {msg}", *args, **kwargs)

    def refresh_access_token(self, _: dict[str, Any]) -> None:
        """Refresh the access token."""
        self.log("Refreshing access token", self.bank)

        try:
            self.client.refresh_access_token()
        except (HTTPError, RuntimeError) as err:
            if self._handle_credential_error(err):
                return
            raise
        self.log("Refreshed access token")
