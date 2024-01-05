"""Commit the version file to GitHub."""

from __future__ import annotations

import re
from functools import lru_cache
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final, Literal

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from github import Github, InputGitAuthor
from github.Auth import Token
from github.Branch import Branch
from github.GithubException import GithubException, UnknownObjectException
from github.PullRequest import PullRequest
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

    branch: Branch | None
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

    def _process_lovelace_file(self, file: Path) -> None:
        file_content = file.read_text(encoding="utf-8").strip()
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
        else:
            if isinstance(remote_file, list):
                raise TypeError(remote_file)

            if (
                remote_file
                and file_content == remote_file.decoded_content.decode("utf-8").strip()
            ):
                return

        prefix = "Upd" if remote_file else "Cre"
        commit_message = f"{prefix}ate `{repo_file}`"

        self.log("Creating commit with message: %s", commit_message)

        file_content += "\n"

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
            if not self.branch_exists:
                self.repo.create_git_ref(
                    ref=f"refs/heads/{self.BRANCH_NAME}",
                    sha=self.repo.get_git_ref("heads/main").object.sha,
                )

                branch_exists.cache_clear()

            self.repo.create_file(
                path=repo_file,
                message=commit_message,
                content=file_content,
                branch=self.BRANCH_NAME,
                author=self.github_author,
                committer=self.github_author,
            )

        if (pr := self.pull_request) is None:
            self.log("Creating pull request")
            pr = self.repo.create_pull(
                title="Update UI Lovelace Dashboard Files",
                head=self.BRANCH_NAME,
                base="main",
                draft=False,
            )
            pr.set_labels("chore", "ha:lovelace", "non-functional", "tools")

            pr_prefix = "cre"
        else:
            pr_prefix = "upd"

        self.log(pr.html_url)

        self.persistent_notification(
            title="Lovelace UI Dashboard Files Updated",
            id=f"lovelace_ui_dashboard_files_updated_{pr_prefix}",
            message=f"A **[pull request]({pr.html_url})** has been {pr_prefix.lower()}ated for the"
            " UI Lovelace dashboards.",
        )

    def commit_lovelace_files(
        self,
        _: Literal["folder_watcher"],
        data: dict[str, Any],
        ___: dict[str, str],
    ) -> None:
        """Commit the version file to GitHub on startup."""
        if data and not self.LOVELACE_FILE_PATTERN.fullmatch(
            data.get("dest_file", data.get("file", "")),
        ):
            return

        if not (
            lovelace_files := [
                file
                for file in self.STORAGE_DIRECTORY.rglob("*")
                if self.LOVELACE_FILE_PATTERN.fullmatch(file.name)
            ]
        ):
            return

        branch_exists.cache_clear()

        for file in lovelace_files:
            self._process_lovelace_file(file)

    @property
    def branch_exists(self) -> bool:
        """Return whether the branch exists."""
        return branch_exists(self.BRANCH_NAME, self.repo)

    @property
    def pull_request(self) -> PullRequest | None:
        """Return the pull request if it exists."""
        if (pr := pull_request(self.BRANCH_NAME, self.repo)) is None:
            pull_request.cache_clear() # Don't cache None

            self.log("Pull request does not exist")
        else:
            pr.update() # Check it's not an old PR that's since been closed

            if pr.state == "closed":
                pull_request.cache_clear()
                self.log("Pull request is closed, refreshing")
                return self.pull_request

        return pr


@lru_cache(maxsize=1)
def branch_exists(branch_name: str, repo: Repository) -> bool:
    """Return whether the branch exists."""
    return any(branch.name == branch_name for branch in repo.get_branches())


@lru_cache(maxsize=1)
def pull_request(branch_name: str, repo: Repository) -> PullRequest | None:
    """Return whether the pull request exists."""
    for pr in repo.get_pulls(state="open"):
        if pr.head.ref == branch_name:
            return pr

    return None
