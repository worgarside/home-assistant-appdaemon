"""Automatically save money based on certain criteria."""
from __future__ import annotations

from datetime import datetime, timedelta
from json import dumps
from pathlib import Path
from re import IGNORECASE, Pattern
from re import compile as re_compile
from typing import Any, Final, Literal

from appdaemon.entity import Entity  # type: ignore[import-not-found]
from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from wg_utilities.clients import MonzoClient, SpotifyClient, TrueLayerClient
from wg_utilities.clients.monzo import Pot
from wg_utilities.clients.monzo import Transaction as MonzoTransaction
from wg_utilities.clients.truelayer import Bank, Card
from wg_utilities.clients.truelayer import Transaction as TrueLayerTransaction
from wg_utilities.loggers import add_warehouse_handler

CACHE_DIR = Path("/config/.wg-utilities/oauth_credentials")


class AutoSaver(Hass):  # type: ignore[misc]
    """Automatically save money based on certain criteria."""

    AUTO_SAVE_VARIABLE_ID: Final[str] = "var.auto_save_amount"

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

        truelayer_client_id = self.args["truelayer_client_id"]

        self.amex_card = TrueLayerClient(
            client_id=truelayer_client_id,
            client_secret=self.args["truelayer_client_secret"],
            creds_cache_path=CACHE_DIR.joinpath(
                "TrueLayerClient",
                truelayer_client_id,
                "amex_auto_saver.json",
            ),
            use_existing_credentials_only=True,
            bank=Bank.AMEX,
        ).list_cards()[0]

        self.monzo_client = MonzoClient(
            client_id=self.args["monzo_client_id"],
            client_secret=self.args["monzo_client_secret"],
            creds_cache_dir=CACHE_DIR,
            use_existing_credentials_only=True,
        )

        if not (
            savings_pot := self.monzo_client.get_pot_by_id(self.args["savings_pot_id"])
        ):
            self.error("Could not find savings pot")
            raise RuntimeError("Could not find savings pot")  # noqa: TRY003

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
            creds_cache_dir=Path("/config/.wg-utilities/oauth_credentials"),
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
            if tx.amount <= 0:
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

        naughty_txs = self.naughty_transactions

        amount = sum(transaction.amount for transaction in naughty_txs)  # + GBP

        return (
            int(self.naughty_transaction_percentage * amount * 100),  # pence
            [f"£{tx.amount} @ {tx.description}" for tx in naughty_txs],
        )

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

        attributes["Breakdown"] = dumps(breakdown)

        self.call_service(
            "var/set",
            entity_id=self.AUTO_SAVE_VARIABLE_ID,
            value=round(auto_save_amount / 100, 2),
            force_update=False,
            attributes=attributes,
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
            datetime=datetime.utcnow().isoformat(),
        )

    def update_transaction_records(self) -> None:
        """Get the newest transactions from Amex/Monzo."""
        for transactions, timestamp_attr, sort_by, get_transactions in (
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

            recent_transactions = get_transactions(from_datetime=from_datetime)  # type: ignore[operator]

            transactions.extend(recent_transactions)

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
    def naughty_transactions(self) -> list[TrueLayerTransaction]:
        """Get the list of naughty transactions on my Amex card.

        Only transactions since the last auto-save are returned.
        """
        if self.naughty_transaction_pattern is None:
            return []

        return [
            tx
            for tx in self.amex_transactions
            if self.naughty_transaction_pattern.search(tx.description)
            and tx.amount > 0  # GBP
        ]

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
