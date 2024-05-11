"""Commit the version file to GitHub."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from github import Github, InputGitAuthor
from github.Auth import Token
from wg_utilities.loggers import add_warehouse_handler

if TYPE_CHECKING:
    from github.Repository import Repository

REPO_NAME: Final[str] = "worgarside/home-assistant"


class VersionFileCommitter(Hass):  # type: ignore[misc]
    """AppDaemon app to commit the version file to GitHub."""

    VERSION_FILE_PATH: Final[Path] = Path("/homeassistant/.HA_VERSION")

    github_author: InputGitAuthor
    repo: Repository

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.repo = Github(auth=Token(self.args["github_token"])).get_repo(REPO_NAME)

        self.github_author = InputGitAuthor(
            name="Home Assistant",
            email=self.args.get("github_email", ""),
        )

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

        self.log("Local version: %s", local_version)

        remote_file = self.repo.get_contents(".HA_VERSION")

        if isinstance(remote_file, list):
            raise TypeError(remote_file)

        remote_version = remote_file.decoded_content.decode("utf-8").strip()

        self.log("Remote version: %s", remote_version)

        if local_version != remote_version:
            branch_name = f"chore/home-assistant-{local_version}"

            if any(branch.name == branch_name for branch in self.repo.get_branches()):
                self.log("Branch `%s` already exists", branch_name)
                return

            ref = self.repo.get_git_ref("heads/main")
            self.repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=ref.object.sha,
            )

            commit_message = f"Bump Home Assistant version to `{local_version}`"

            self.log("Creating commit with message: %s", commit_message)

            self.repo.update_file(
                ".HA_VERSION",
                commit_message,
                local_version + "\n",
                remote_file.sha,
                branch=branch_name,
                author=self.github_author,
                committer=self.github_author,
            )

            self.log("Creating pull request")

            pr = self.repo.create_pull(
                title=commit_message,
                head=branch_name,
                base="main",
                draft=False,
            )

            pr.set_labels("non-functional", "tools")

            self.log(pr.html_url)

            self.persistent_notification(
                title="Home Assistant Version Bump",
                message=f"A [pull request]({pr.html_url}) has been created to bump the current"
                f" Home Assistant version from {remote_version} to {local_version}.",
            )
