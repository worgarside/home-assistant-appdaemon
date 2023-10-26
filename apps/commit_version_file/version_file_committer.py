"""Commit the version file to GitHub."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from github import Auth, Github
from wg_utilities.loggers import add_warehouse_handler

REPO_NAME = "worgarside/home-assistant"


class VersionFileCommitter(Hass):  # type: ignore[misc]
    """AppDaemon app to commit the version file to GitHub."""

    VERSION_FILE_PATH = Path("/config/.HA_VERSION")

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.listen_event(self.commit_version_file, "homeassistant_start")

        # Run on app init to cover system reboot
        self.commit_version_file("homeassistant_start", {}, {})

    def commit_version_file(
        self,
        _: Literal["homeassistant_start"],
        __: dict[str, Any],
        ___: dict[str, str],
    ) -> None:
        """Commit the version file to GitHub on startup."""
        local_version = self.VERSION_FILE_PATH.read_text(encoding="utf-8").strip()

        github = Github(auth=Auth.Token(self.args["github_token"]))
        repo = github.get_repo(REPO_NAME)

        remote_file = repo.get_contents(".HA_VERSION")

        if isinstance(remote_file, list):
            raise TypeError(remote_file)

        remote_version = remote_file.decoded_content.decode("utf-8").strip()

        if local_version != remote_version:
            branch_name = f"chore/home-assistant-{local_version}"

            if not any(branch.name == branch_name for branch in repo.get_branches()):
                ref = repo.get_git_ref("heads/main")
                repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=ref.object.sha)

                commit_message = f"Bump Home Assistant version to `{local_version}`"
                repo.update_file(
                    ".HA_VERSION",
                    commit_message,
                    local_version + "\n",
                    remote_file.sha,
                    branch=branch_name,
                )

                pr = repo.create_pull(
                    title=commit_message,
                    head=branch_name,
                    base="main",
                    draft=False,
                )

                self.persistent_notification(
                    title="Home Assistant Version Bump",
                    message=f"A [pull request]({pr.html_url}) has been created to bump the Home"
                    f" Assistant current version from {remote_version} to {local_version}.",
                )
