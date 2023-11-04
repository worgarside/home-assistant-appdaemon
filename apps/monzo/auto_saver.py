"""Automatically save money based on certain criteria."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from appdaemon.entity import Entity  # type: ignore[import-not-found]
from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from wg_utilities.clients import MonzoClient
from wg_utilities.clients.monzo import Pot, Transaction
from wg_utilities.loggers import add_warehouse_handler


class AutoSaver(Hass):  # type: ignore[misc]
    """Automatically save money based on certain criteria."""

    AUTO_SAVE_VARIABLE_ID: Final[str] = "var.auto_save_amount"

    client: MonzoClient
    auto_save_minimum: Entity
    debit_transaction_percentage: Entity
    last_auto_save: Entity
    savings_pot: Pot

    transactions: list[Transaction]

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.client = MonzoClient(
            client_id=self.args["client_id"],
            client_secret=self.args["client_secret"],
            creds_cache_dir=Path("/config/.wg-utilities/oauth_credentials"),
            use_existing_credentials_only=True,
        )

        if not (savings_pot := self.client.get_pot_by_name("savings")):
            self.error("Could not find savings pot")
            raise RuntimeError("Could not find savings pot")  # noqa: TRY003

        self.savings_pot = savings_pot

        self.auto_save_minimum = self.get_entity("input_number.auto_save_minimum")
        self.debit_transaction_percentage = self.get_entity(
            "input_number.auto_save_debit_transaction_percentage",
        )
        self.last_auto_save = self.get_entity("input_datetime.last_auto_save")
        self.transactions = []

        self.listen_state(
            self.calculate,
            [
                self.auto_save_minimum.entity_id,
                self.debit_transaction_percentage.entity_id,
                self.last_auto_save.entity_id,
            ],
        )

        self.log("Listen state registered for %s", self.savings_pot.name)

        self.calculate(
            "",
            "state",
            "",
            "-",
            {},
        )

    def _get_round_up_pence(self) -> int:
        """Sum the round-up amounts from a list of transactions.

        Transactions at integer pound values will result in a round-up of 100p.
        """
        return sum(
            100 - int(str(transaction.amount)[-2:]) for transaction in self.transactions
        )

    def _get_percentage_of_debit_transactions(
        self,
    ) -> int:
        """Get the percentage of income to save."""
        percentage = float(self.debit_transaction_percentage.get_state()) / 100

        return int(
            sum(
                percentage * transaction.amount
                for transaction in self.transactions
                if transaction.amount > 0
            ),
        )

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
            datetime.strptime(
                self.last_auto_save.get_state(),
                "%Y-%m-%d %H:%M:%S",
            )
            if not self.transactions
            else max(
                self.transactions,
                key=lambda transaction: transaction.created,
            ).created
            + timedelta(seconds=1)
        )

        recent_transactions = self.client.current_account.list_transactions(
            from_datetime=from_datetime,
        )

        self.transactions.extend(recent_transactions)

        self.log(
            "Found %s new transactions (%i total)",
            len(recent_transactions),
            len(self.transactions),
        )

        auto_save_amount = sum(
            [
                self._get_round_up_pence(),
                self._get_percentage_of_debit_transactions(),
            ],
        ) + (float(self.auto_save_minimum.get_state()) * 100)

        self.log("Auto-save amount is %s", auto_save_amount)

        self.call_service(
            "var/set",
            entity_id=self.AUTO_SAVE_VARIABLE_ID,
            value=round(auto_save_amount / 100, 2),
            force_update=True,
        )
