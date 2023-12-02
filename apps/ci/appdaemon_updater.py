"""AppDaemon app to update AppDaemon to the latest release."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from appdaemon.entity import Entity  # type: ignore[import-not-found]
from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from git import GitCommandError, Repo


class Updater(Hass):  # type: ignore[misc]
    """AppDaemon app to update AppDaemon to the latest release."""

    REPO: ClassVar[Repo] = Repo("/config")

    _current_branch: Entity
    _current_ref: Entity

    def initialize(self) -> None:
        """Initialize the app."""
        self._current_branch = self.get_entity("var.current_appdaemon_branch")
        self._current_ref = self.get_entity("var.current_appdaemon_ref")

        self.listen_state(
            self.get_latest_release,
            "input_text.ad_get_latest_release",
        )

        self.run_every(self.update_variables, "now", 2 * 60)

    def update_variables(self, _: dict[str, Any] | None = None) -> None:
        """Update the variables with the current AppDaemon ref and branch."""
        self.current_branch = self.REPO.active_branch.name

        try:
            self.current_ref = self.REPO.git.describe(tags=True, exact_match=True)
        except GitCommandError:
            self.current_ref = self.REPO.head.commit.hexsha[:7]

    def get_latest_release(
        self,
        entity: Literal["input_text.ad_get_latest_release"],
        attribute: Literal["state"],
        old: str,
        new: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Get the latest AppDaemon release from GitHub."""
        del entity, attribute, kwargs

        if old in (new, "unavailable") or not old:
            return

        self.REPO.git.fetch("--all", "--tags", "--prune")

        for tag in self.REPO.tags:
            if tag.name == new:
                self.REPO.git.add(A=True)

                self.REPO.git.stash(
                    "save",
                    f"Stashing changes before updating to {new} | {self.datetime()!s}",
                )

                try:
                    self.REPO.git.checkout(new)
                except GitCommandError as exc:
                    self.error(f"Error checking out {new}: {exc!s}")
                    raise

                self.update_variables()
                break
        else:
            self.error(f"Unable to find tag {new!r}")

    @property
    def current_ref(self) -> str:
        """Get the current AppDaemon ref."""
        return str(self._current_ref.get_state())

    @current_ref.setter
    def current_ref(self, new: str, /) -> None:
        self.call_service(
            "var/set",
            entity_id=self._current_ref.entity_id,
            value=new,
            force_update=True,
        )

    @property
    def current_branch(self) -> str:
        """Get the current AppDaemon branch."""
        return str(self._current_branch.get_state())

    @current_branch.setter
    def current_branch(self, new: str, /) -> None:
        self.call_service(
            "var/set",
            entity_id=self._current_branch.entity_id,
            value=new,
            force_update=True,
        )
