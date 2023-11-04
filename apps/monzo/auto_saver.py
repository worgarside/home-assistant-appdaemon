"""Automatically save money based on certain criteria."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from appdaemon.entity import Entity  # type: ignore[import-not-found]
from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from wg_utilities.clients import MonzoClient, SpotifyClient
from wg_utilities.clients.monzo import Pot, Transaction
from wg_utilities.loggers import add_warehouse_handler


class AutoSaver(Hass):  # type: ignore[misc]
    """Automatically save money based on certain criteria."""

    AUTO_SAVE_VARIABLE_ID: Final[str] = "var.auto_save_amount"

    _auto_save_minimum: Entity
    _debit_transaction_percentage: Entity
    _last_auto_save: Entity
    monzo_client: MonzoClient
    savings_pot: Pot
    spotify_client: SpotifyClient
    transactions: list[Transaction]

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.monzo_client = MonzoClient(
            client_id=self.args["monzo_client_id"],
            client_secret=self.args["monzo_client_secret"],
            creds_cache_dir=Path("/config/.wg-utilities/oauth_credentials"),
            use_existing_credentials_only=True,
        )

        if not (savings_pot := self.monzo_client.get_pot_by_name("savings")):
            self.error("Could not find savings pot")
            raise RuntimeError("Could not find savings pot")  # noqa: TRY003

        self.savings_pot = savings_pot
        self.transactions = []

        self._auto_save_minimum = self.get_entity("input_number.auto_save_minimum")
        self._debit_transaction_percentage = self.get_entity(
            "input_number.auto_save_debit_transaction_percentage",
        )
        self._last_auto_save = self.get_entity("input_datetime.last_auto_save")

        self.listen_state(
            self.calculate,
            [
                self._auto_save_minimum.entity_id,
                self._debit_transaction_percentage.entity_id,
                self._last_auto_save.entity_id,
            ],
        )

        self.log("Listen state registered for %s", self.savings_pot.name)

        self.spotify_client = SpotifyClient(
            client_id=self.args["spotify_client_id"],
            client_secret=self.args["spotify_client_secret"],
            creds_cache_dir=Path("/config/.wg-utilities/oauth_credentials"),
            use_existing_credentials_only=True,
        )

        self.calculate(
            "",
            "state",
            "",
            "-",
            {},
        )

    def _get_percentage_of_debit_transactions(
        self,
    ) -> int:
        """Get the percentage of income to save."""
        return int(
            sum(
                self.debit_transaction_percentage * transaction.amount
                for transaction in self.transactions
                if transaction.amount > 0
            ),
        )

    def _get_round_up_pence(self) -> int:
        """Sum the round-up amounts from a list of transactions.

        Transactions at integer pound values will result in a round-up of 100p.
        """
        return sum(
            100 - int(str(transaction.amount)[-2:]) for transaction in self.transactions
        )

    def _get_spotify_savings(self) -> int:
        """'Pay' 79p a song to savings."""
        liked_songs = [
            track
            for track in self.spotify_client.current_user.get_recently_liked_tracks(
                day_limit=(datetime.utcnow() - self.last_auto_save).days + 1,
            )
            if track.metadata["saved_at"] >= self.last_auto_save
        ]

        return 79 * len(liked_songs)

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

        from_datetime = (
            self.last_auto_save
            if not self.transactions
            else max(
                self.transactions,
                key=lambda transaction: transaction.created,
            ).created
            + timedelta(seconds=1)
        )

        recent_transactions = self.monzo_client.current_account.list_transactions(
            from_datetime=from_datetime,
        )

        self.transactions.extend(recent_transactions)

        self.log(
            "Found %s new transactions (%i total)",
            len(recent_transactions),
            len(self.transactions),
        )

        auto_save_amount = (
            sum(
                [
                    self._get_round_up_pence(),
                    self._get_percentage_of_debit_transactions(),
                    self._get_spotify_savings(),
                ],
            )
            + self.auto_save_minimum
        )

        self.log("Auto-save amount is %s", auto_save_amount)

        self.call_service(
            "var/set",
            entity_id=self.AUTO_SAVE_VARIABLE_ID,
            value=round(auto_save_amount / 100, 2),
            force_update=True,
        )

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
