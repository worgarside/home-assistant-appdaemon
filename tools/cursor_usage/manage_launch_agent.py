"""Install or remove the Cursor usage token launch agent on macOS."""

from __future__ import annotations

# ruff: noqa: T201
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

LABEL: Final[str] = "com.worgarside.cursor-usage-token-push"
SOURCE_SCRIPT: Final[Path] = Path(__file__).with_name("cursor_token_push.py")
INSTALLED_SCRIPT: Final[Path] = Path.home() / ".local/bin/cursor-token-push.py"
LAUNCH_AGENT: Final[Path] = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
STDOUT_LOG: Final[Path] = Path.home() / "Library/Logs/cursor-usage-token-push.log"
STDERR_LOG: Final[Path] = Path.home() / "Library/Logs/cursor-usage-token-push.error.log"


def _launch_domain() -> str:
    """Return the current user's launchd domain."""
    return f"gui/{os.getuid()}"


def _run_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run launchctl without invoking a shell."""
    return subprocess.run(  # noqa: S603
        ["/bin/launchctl", *arguments],
        check=True,
        text=True,
        capture_output=True,
    )


def _try_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run launchctl and return failures to the caller."""
    return subprocess.run(  # noqa: S603
        ["/bin/launchctl", *arguments],
        check=False,
        text=True,
        capture_output=True,
    )


def _unload() -> None:
    """Unload the launch agent if it is currently registered."""
    if not LAUNCH_AGENT.exists():
        return
    _try_launchctl(
        "bootout",
        _launch_domain(),
        str(LAUNCH_AGENT),
    )


def _plist(webhook_url: str) -> dict[str, object]:
    """Build the launch agent property list."""
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(INSTALLED_SCRIPT)],
        "EnvironmentVariables": {"CURSOR_USAGE_WEBHOOK_URL": webhook_url},
        "RunAtLoad": True,
        "StartInterval": 6 * 60 * 60,
        "StandardOutPath": str(STDOUT_LOG),
        "StandardErrorPath": str(STDERR_LOG),
    }


def setup(webhook_url: str) -> None:
    """Install and load the launch agent."""
    if sys.platform != "darwin":
        raise RuntimeError("The Cursor token launch agent requires macOS")
    if not SOURCE_SCRIPT.exists():
        raise RuntimeError(f"Runtime script is missing: {SOURCE_SCRIPT}")

    subprocess.run(  # noqa: S603
        ["/usr/bin/python3", str(SOURCE_SCRIPT), "--check"],
        check=True,
    )

    INSTALLED_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)

    _unload()
    shutil.copy2(SOURCE_SCRIPT, INSTALLED_SCRIPT)
    INSTALLED_SCRIPT.chmod(0o755)
    with LAUNCH_AGENT.open("wb") as plist_file:
        plistlib.dump(_plist(webhook_url), plist_file, sort_keys=False)

    try:
        _run_launchctl("bootstrap", _launch_domain(), str(LAUNCH_AGENT))
    except subprocess.CalledProcessError as error:
        details = error.stderr.strip() or error.stdout.strip()
        raise RuntimeError(f"launchctl bootstrap failed: {details}") from error

    print(f"Installed and loaded {LABEL}")
    print(f"Webhook: {webhook_url}")


def teardown() -> None:
    """Unload the launch agent and remove everything it installed."""
    _unload()
    for path in (LAUNCH_AGENT, INSTALLED_SCRIPT, STDOUT_LOG, STDERR_LOG):
        path.unlink(missing_ok=True)
    print(f"Unloaded and removed {LABEL}")


def status() -> None:
    """Show whether the launch agent is loaded and installed."""
    result = _try_launchctl(
        "print",
        f"{_launch_domain()}/{LABEL}",
    )
    state = "loaded" if result.returncode == 0 else "not loaded"
    installed = "installed" if LAUNCH_AGENT.exists() else "not installed"
    print(f"{LABEL}: {state}; {installed}")


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument("webhook_url")
    subparsers.add_parser("teardown")
    subparsers.add_parser("status")
    return parser.parse_args()


def main() -> int:
    """Run the requested launch agent operation."""
    args = _parse_args()
    try:
        if args.command == "setup":
            setup(str(args.webhook_url))
        elif args.command == "teardown":
            teardown()
        else:
            status()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Launch agent operation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
