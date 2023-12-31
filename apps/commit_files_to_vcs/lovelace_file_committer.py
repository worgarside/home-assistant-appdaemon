"""Commit the version file to GitHub."""

from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final, Literal

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from github import Github, InputGitAuthor
from github.Auth import Token
from github.GithubException import GithubException, UnknownObjectException
from github.Repository import Repository
from wg_utilities.loggers import add_warehouse_handler

REPO_NAME: Final[str] = "worgarside/home-assistant"


class LovelaceFileCommitter(Hass):  # type: ignore[misc]
    """AppDaemon app to commit the version file to GitHub."""

    BRANCH_NAME: Final[
        Literal["chore/lovelace-ui-dashboards"]
    ] = "chore/lovelace-ui-dashboards"
    LOVELACE_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(
        r"^lovelace(_dashboards|\..+)$",
    )
    STORAGE_DIRECTORY: Final[Path] = Path("/homeassistant/.storage")
    REPO_DIRECTORY: Final[Path] = Path("lovelace/dashboards/ui_only")

    github_author: InputGitAuthor
    repo: Repository

    def initialize(self) -> None:
        """Initialize the app."""
        add_warehouse_handler(self.err)

        self.repo = Github(auth=Token(self.args["github_token"])).get_repo(
            REPO_NAME,
        )

        self.github_author = InputGitAuthor(
            name="Home Assistant",
            email=self.args.get("github_email", ""),
        )

        self.listen_event(self.commit_lovelace_files, "folder_watcher")

        # Run on app init to cover bugfixes/restarts etc.
        self.commit_lovelace_files("folder_watcher", {}, {})

    def _process_lovelace_file(self, file: Path) -> bool | None:
        file_content = file.read_text(encoding="utf-8").strip() + "\n"

        repo_file = self.REPO_DIRECTORY.joinpath(file.name + ".json").as_posix()

        self.log("Processing Lovelace file: %s", repo_file)

        try:
            remote_file = self.repo.get_contents(repo_file, ref=self.BRANCH_NAME)
        except UnknownObjectException:
            remote_file = None
        except GithubException as e:
            if e.status != HTTPStatus.NOT_FOUND:
                raise

            remote_file = None

        if isinstance(remote_file, list):
            raise TypeError(remote_file)

        if remote_file is not None:
            remote_content = remote_file.decoded_content.decode("utf-8").strip() + "\n"

            if file_content == remote_content:
                return None

        for branch in self.repo.get_branches():
            if branch.name == self.BRANCH_NAME:
                branch_created = False
                break
        else:
            branch_created = True
            ref = self.repo.get_git_ref("heads/main")
            self.repo.create_git_ref(
                ref=f"refs/heads/{self.BRANCH_NAME}",
                sha=ref.object.sha,
            )

        prefix = "Upd" if remote_file else "Cre"
        commit_message = f"{prefix}ate `{repo_file}`"

        self.log("%sating commit with message: %s", prefix.title(), commit_message)

        if remote_file:
            self.repo.update_file(
                path=repo_file,
                message=commit_message,
                content=file_content,
                sha=remote_file.sha,
                branch=self.BRANCH_NAME,
                author=self.github_author,
                committer=self.github_author,
            )
        else:
            self.repo.create_file(
                path=repo_file,
                message=commit_message,
                content=file_content,
                branch=self.BRANCH_NAME,
                author=self.github_author,
                committer=self.github_author,
            )

        return branch_created

    def commit_lovelace_files(
        self,
        _: Literal["folder_watcher"],
        data: dict[str, Any],
        ___: dict[str, str],
    ) -> None:
        """Commit the version file to GitHub on startup."""
        self.log(data)

        if data and not self.LOVELACE_FILE_PATTERN.fullmatch(
            data.get("dest_file", data.get("file", "")),
        ):
            return

        branch_created = False
        some_change_made = False

        for file in self.STORAGE_DIRECTORY.rglob("*"):
            if not self.LOVELACE_FILE_PATTERN.fullmatch(file.name):
                continue

            self.log("Found Lovelace file: %s", file.as_posix())

            if (branch_created_for_file := self._process_lovelace_file(file)) is None:
                continue

            some_change_made = True
            branch_created = branch_created or branch_created_for_file

        if not some_change_made:
            self.log("No changes made")
            return

        if branch_created:
            self.log("Creating pull request")

            pr = self.repo.create_pull(
                title="Update UI Lovelace Dashboard Files",
                head=self.BRANCH_NAME,
                base="main",
                draft=False,
            )
        else:
            matching_prs = [
                pr
                for pr in self.repo.get_pulls(state="open")
                if pr.head.ref == self.BRANCH_NAME and pr.base.ref == "main"
            ]

            self.log("Found %d matching PRs: %s", len(matching_prs), matching_prs)

            if matching_prs:
                pr = matching_prs[0]
            else:
                raise RuntimeError("No matching PRs found")  # noqa: TRY003

        pr.set_labels("chore", "ha:lovelace", "non-functional", "tools")

        self.log(pr.html_url)

        self.persistent_notification(
            title="Lovelace UI Dashboard Files Updated",
            id="lovelace_ui_dashboard_files_updated",
            message=f"A **[pull request]({pr.html_url})** has been {'cre' if branch_created else 'upd'}ated for the"
            " UI Lovelace dashboards.",
        )
