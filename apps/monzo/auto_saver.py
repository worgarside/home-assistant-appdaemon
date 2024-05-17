"""Automatically save money based on certain criteria."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from json import dumps
from pathlib import Path
from re import IGNORECASE, Pattern
from re import compile as re_compile
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib import parse

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from requests import HTTPError
from wg_utilities.clients import MonzoClient, SpotifyClient, TrueLayerClient
from wg_utilities.clients.oauth_client import OAuthCredentials
from wg_utilities.clients.truelayer import Bank, Card
from wg_utilities.clients.truelayer import Transaction as TrueLayerTransaction
from wg_utilities.loggers import add_warehouse_handler

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from appdaemon.entity import Entity  # type: ignore[import-not-found]
    from wg_utilities.clients.monzo import Pot
    from wg_utilities.clients.monzo import Transaction as MonzoTransaction

CACHE_DIR = Path("/homeassistant/.wg-utilities/oauth_credentials")


class AutoSaver(Hass):  # type: ignore[misc]
    """Automatically save money based on certain criteria."""

    AUTO_SAVE_VARIABLE_ID: Final[str] = "var.auto_save_amount"
    MULTISPACE_PATTERN: Final[Pattern[str]] = re_compile(r"\s+")

    _auto_save_minimum: Entity
    _debit_transaction_percentage: Entity
    _last_auto_save: Entity
    _naughty_transaction_pattern: Entity
    _naughty_transaction_percentage: Entity

    _amex_transactions: list[TrueLayerTransaction]
    _monzo_transactions: list[MonzoTransaction]

    amex_card: Card
    monzo_client: MonzoClient
    savings_pot: Pot
    spotify_client: SpotifyClient

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.redirect_uri = "http://localhost:5000/get_auth_code"

        self.monzo_client = MonzoClient(
            client_id=self.args["monzo_client_id"],
            client_secret=self.args["monzo_client_secret"],
            creds_cache_dir=CACHE_DIR,
            use_existing_credentials_only=True,
        )

        truelayer_client_id = self.args["truelayer_client_id"]

        self.truelayer_client = TrueLayerClient(
            client_id=truelayer_client_id,
            client_secret=self.args["truelayer_client_secret"],
            creds_cache_path=CACHE_DIR.joinpath(
                TrueLayerClient.__name__,
                truelayer_client_id,
                "amex_auto_saver.json",
            ),
            use_existing_credentials_only=True,
            bank=Bank.AMEX,
        )

        self.auth_code_input_text_lookup: dict[MonzoClient | TrueLayerClient, str] = {
            self.monzo_client: "input_text.monzo_auth_token_auto_saver",
            self.truelayer_client: f"input_text.truelayer_{self.truelayer_client.bank.name.lower()}_auto_saver_auth_token",  # noqa: E501
        }

        self.input_text_client_lookup: dict[str, MonzoClient | TrueLayerClient] = {
            v: k for k, v in self.auth_code_input_text_lookup.items()
        }

        self.initialize_entities()

    def initialize_entities(self) -> None:
        """Initialize the entities."""
        try:
            self.amex_card = self.truelayer_client.list_cards()[0]
        except HTTPError as err:
            if (
                err.response.url == self.truelayer_client.ACCESS_TOKEN_ENDPOINT
                and err.response.status_code == HTTPStatus.BAD_REQUEST
            ):
                try:
                    self.send_auth_link_notification(self.truelayer_client)
                except Exception as login_err:
                    raise login_err from err
                else:
                    return

    def _initialize_entities(self) -> None:
        if not (
            savings_pot := self.monzo_client.get_pot_by_id(self.args["savings_pot_id"])
        ):
            self.error("Could not find savings pot")
            raise RuntimeError("Could not find savings pot")

        self.savings_pot = savings_pot

        self._amex_transactions = []
        self._monzo_transactions = []

        self._auto_save_minimum = self.get_entity("input_number.auto_save_minimum")
        self._debit_transaction_percentage = self.get_entity(
            "input_number.auto_save_debit_transaction_percentage",
        )
        self._last_auto_save = self.get_entity("input_datetime.last_auto_save")
        self._naughty_transaction_pattern = self.get_entity(
            "input_text.auto_save_naughty_transaction_pattern",
        )
        self._naughty_transaction_percentage = self.get_entity(
            "input_number.auto_save_naughty_transaction_percentage",
        )

        self.spotify_client = SpotifyClient(
            client_id=self.args["spotify_client_id"],
            client_secret=self.args["spotify_client_secret"],
            creds_cache_dir=Path("/homeassistant/.wg-utilities/oauth_credentials"),
            use_existing_credentials_only=True,
        )

        self.listen_state(
            self.calculate,
            listen_entities := [
                self._auto_save_minimum.entity_id,
                self._debit_transaction_percentage.entity_id,
                self._last_auto_save.entity_id,
                self._naughty_transaction_pattern.entity_id,
                self._naughty_transaction_percentage.entity_id,
                "var.truelayer_balance_monzo_current_account",
            ],
        )

        self.log(
            "Listen states registered for %i entities: %s",
            len(listen_entities),
            ", ".join(listen_entities),
        )

        self.listen_state(
            self.save_money,
            "input_boolean.ad_monzo_auto_save",
        )

        self.calculate(
            "",
            "state",
            "",
            "-",
            {},
        )

    def _get_percentage_of_debit_transactions_value(self) -> tuple[int, list[str]]:
        """Get the percentage of income to save."""
        amount = 0
        breakdown = []

        for tx in self.monzo_transactions:
            # Ignore credit transactions or pot withdrawals
            if tx.amount <= 0 or tx.description.startswith("pot_"):
                continue

            amount += tx.amount
            breakdown.append(f"£{tx.amount/100:.2f} @ {tx.description}")

        return int(self.debit_transaction_percentage * amount), breakdown

    def _get_percentage_of_naughty_transactions_value(
        self,
    ) -> tuple[int, list[str]]:
        """Get the amount to save from naughty transactions."""
        if self.naughty_transaction_pattern is None:
            return 0, ["No pattern set!"]

        amex_subtotal = 0.0  # +GBP
        monzo_subtotal = 0  # -pence

        breakdown = []

        for atx in self.amex_transactions:
            if (
                self.naughty_transaction_pattern.search(atx.description)
                and atx.amount > 0
            ):  # GBP
                amex_subtotal += atx.amount

                breakdown.append(
                    f"£{atx.amount:.2f} @ {self.MULTISPACE_PATTERN.sub(' ', atx.description)}",
                )

        for mtx in self.monzo_transactions:
            if (
                self.naughty_transaction_pattern.search(mtx.description)
                and mtx.amount < 0
            ):  # pence
                monzo_subtotal += mtx.amount

                breakdown.append(
                    f"£{-mtx.amount/100:.2f} @ {self.MULTISPACE_PATTERN.sub(' ', mtx.description)}",
                )

        # subtract because Monzo transactions are negative in value
        amount_total = int(
            ((amex_subtotal * 100) - monzo_subtotal)
            * self.naughty_transaction_percentage,
        )

        return amount_total, breakdown

    def _get_round_up_pence(self) -> tuple[int, None]:
        """Sum the round-up amounts from a list of transactions.

        Transactions at integer pound values will result in a round-up of 100p.

        Returns a value in pence.
        """
        return (
            int(
                sum(
                    (100 - (-transaction.amount % 100))  # - pence
                    for transaction in self.monzo_transactions
                )
                + sum(
                    100 - ((transaction.amount * 100) % 100)  # + GBP
                    for transaction in self.amex_transactions
                ),
            ),
            None,
        )

    def _get_spotify_savings(self) -> tuple[int, list[str]]:
        """'Pay' 79p a song to savings."""
        liked_tracks = [
            track
            for track in self.spotify_client.current_user.get_recently_liked_tracks(
                day_limit=(datetime.utcnow() - self.last_auto_save).days + 1,
            )
            if track.metadata["saved_at"] >= self.last_auto_save
        ]

        return 79 * len(liked_tracks), [str(track) for track in liked_tracks]

    def calculate(
        self,
        entity: str,
        attribute: Literal["state"],
        old: str,
        new: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Calculate the current auto-save amount."""
        _ = entity, old, kwargs

        if attribute != "state" or not new:
            return

        self.update_transaction_records()

        savings: dict[str, int] = {}
        breakdown: dict[str, dict[str, list[str]] | list[str]] = {}

        for category, saving in (
            (
                "Round Ups",
                self._get_round_up_pence(),
            ),
            (
                "Debit Transaction Percentage",
                self._get_percentage_of_debit_transactions_value(),
            ),
            (
                "Naughty Transaction Percentage",
                self._get_percentage_of_naughty_transactions_value(),
            ),
            (
                "Spotify Tracks",
                self._get_spotify_savings(),
            ),
        ):
            amount, bd = saving

            savings[category] = amount
            if bd:
                breakdown[category] = bd

        savings["Minimum"] = self.auto_save_minimum

        auto_save_amount = sum(savings.values())

        self.log("Auto-save amount is %s", auto_save_amount)

        attributes: dict[str, float | str] = {k: v / 100 for k, v in savings.items()}

        if breakdown:
            attributes["Breakdown"] = dumps(breakdown)

        self.call_service(
            "var/set",
            entity_id=self.AUTO_SAVE_VARIABLE_ID,
            value=round(auto_save_amount / 100, 2),
            force_update=False,
            attributes=attributes,
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
        _ = attribute, old, pin_app, kwargs

        if not new:
            return

        client = self.input_text_client_lookup[entity]

        if len(new) == 1:
            self.send_auth_link_notification(client)
            return

        self.log("Consuming auth code %s", new)

        try:
            credentials: dict[str, Any] = client.post_json_response(  # type: ignore[assignment]
                client.access_token_endpoint,
                json={
                    "code": new,
                    "grant_type": "authorization_code",
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
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

        credentials["client_id"] = client.client_id
        credentials["client_secret"] = client.client_secret

        client.credentials = OAuthCredentials.parse_first_time_login(credentials)

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

    def save_money(
        self,
        entity: Literal["input_boolean.ad_monzo_auto_save"],
        attribute: Literal["state"],
        old: str,
        new: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Save money to the savings pot."""
        _ = entity, attribute, kwargs

        if old == new or ({old, new} & {"unavailable", "unknown"}):
            return

        self.monzo_client.deposit_into_pot(
            self.savings_pot,
            amount_pence=int(float(self.get_state(self.AUTO_SAVE_VARIABLE_ID)) * 100),
            dedupe_id="-".join(
                (
                    self.name,
                    self.get_state(
                        self.AUTO_SAVE_VARIABLE_ID,
                        attribute="last_changed",
                    ),
                ),
            ),
        )

        self.call_service(
            "input_datetime/set_datetime",
            entity_id=self._last_auto_save.entity_id,
            datetime=datetime.now(UTC).isoformat(),
        )

    def send_auth_link_notification(self, client: TrueLayerClient | MonzoClient) -> None:
        """Run the first time login process."""
        self.log("Running first time login")

        auth_link_params = {
            "client_id": client.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": "abcdefghijklmnopqrstuvwxyz",
            "access_type": "offline",
            "prompt": "consent",
            "scope": " ".join(client.scopes),
        }

        auth_link = client.auth_link_base + "?" + parse.urlencode(auth_link_params)

        self.log(auth_link)

        if isinstance(client, TrueLayerClient):
            title = f"{client.bank} (auto-saver) Access Token Expired"
            message = (
                f"TrueLayer access token for {client.bank} (auto-saver) has expired!"
            )
            notification_id = (
                f"truelayer_{client.bank.name.lower()}_auto_saver_access_token_expired"
            )
        elif isinstance(client, MonzoClient):
            title = "Monzo (auto-saver) Access Token Expired"
            message = "Monzo access token has expired!"
            notification_id = "monzo_auto_saver_access_token_expired"
        else:
            raise TypeError(f"Invalid client: {client!r}")

        self.call_service(
            "script/turn_on",
            entity_id="script.notify_will",
            variables={
                "clear_notification": True,
                "title": title,
                "message": message,
                "notification_id": notification_id,
                "mobile_notification_icon": "mdi:key-alert-outline",
                "actions": dumps(
                    [
                        {"action": "URI", "title": "Auth Link", "uri": auth_link},
                        {
                            "action": "URI",
                            "title": "Submit Code",
                            "uri": f"entityId:{self.auth_code_input_text_lookup[client]}",
                        },
                    ],
                ),
            },
        )

    def update_transaction_records(self) -> None:
        """Get the newest transactions from Amex/Monzo."""
        transactions: Collection[TrueLayerTransaction | MonzoTransaction]
        get_transactions: Callable[
            ...,
            Collection[TrueLayerTransaction | MonzoTransaction],
        ]
        for transactions, timestamp_attr, sort_by, get_transactions in (  # type: ignore[assignment]
            (
                self._amex_transactions,
                "timestamp",
                lambda tx: tx.timestamp,
                self.amex_card.get_transactions,
            ),
            (
                self._monzo_transactions,
                "created",
                lambda tx: tx.created,
                self.monzo_client.current_account.list_transactions,
            ),
        ):
            from_datetime = (
                self.last_auto_save
                if not transactions
                else getattr(
                    max(transactions, key=sort_by),
                    timestamp_attr,
                )
                + timedelta(seconds=1)
            )

            recent_transactions = get_transactions(from_datetime=from_datetime)

            transactions.extend(recent_transactions)  # type: ignore[attr-defined]

            self.log(
                "Found %s new transactions (%i total)",
                len(recent_transactions),
                len(transactions),
            )

    @property
    def amex_transactions(self) -> list[TrueLayerTransaction]:
        """Get the list of transactions on my Amex card.

        Only transactions since the last auto-save are returned.

        Amount is positive and in GBP.
        """
        self._amex_transactions = [
            tx
            for tx in self._amex_transactions
            # Use .timestamp() to avoid timezone issues
            if tx.timestamp.timestamp() >= self.last_auto_save.timestamp()
        ]

        return self._amex_transactions

    @property
    def auto_save_minimum(self) -> int:
        """Get the minimum auto-save amount."""
        return int(float(self._auto_save_minimum.get_state()) * 100)

    @property
    def debit_transaction_percentage(self) -> float:
        """Get the percentage of income to save."""
        return float(self._debit_transaction_percentage.get_state()) / 100

    @property
    def last_auto_save(self) -> datetime:
        """Get the date and time of the last auto-save."""
        return datetime.strptime(
            self._last_auto_save.get_state(),
            "%Y-%m-%d %H:%M:%S",
        )

    @property
    def naughty_transaction_pattern(self) -> Pattern[str] | None:
        """Get the regex pattern to match naughty transactions against."""
        if pattern_str := self._naughty_transaction_pattern.get_state():
            return re_compile(
                pattern_str,
                flags=IGNORECASE,
            )

        return None

    @property
    def naughty_transaction_percentage(self) -> float:
        """Get the percentage of naughty transactions to save."""
        return float(self._naughty_transaction_percentage.get_state()) / 100

    @property
    def monzo_transactions(self) -> list[MonzoTransaction]:
        """Get the list of transactions on my Monzo account.

        Only transactions since the last auto-save are returned.

        Amount is negative and in pence.
        """
        self._monzo_transactions = [
            tx
            for tx in self._monzo_transactions
            # Use .timestamp() to avoid timezone issues
            if tx.created.timestamp() >= self.last_auto_save.timestamp()
        ]

        return self._monzo_transactions
