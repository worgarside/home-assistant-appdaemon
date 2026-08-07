"""Push Cursor's current session token to a Home Assistant webhook."""

from __future__ import annotations

# ruff: noqa: T201
import argparse
import base64
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

CURSOR_STATE_DB = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)
JWT_SEPARATOR_COUNT = 2
TOKEN_KEY = "cursorAuth/accessToken"  # noqa: S105
EMAIL_KEY = "cursorAuth/cachedEmail"
PROFILE_KEY = "cursorAuth/cachedScopedProfile"
TEAM_KEY = "cursorAuth/cachedTeam"
WEBHOOK_URL_ENV = "CURSOR_USAGE_WEBHOOK_URL"
PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class CursorAccount:
    """Cached presentation metadata for the active Cursor account."""

    email: str
    display_name: str | None
    team_id: int | None
    team_name: str | None


def _decode_json_object(value: object, key: str) -> dict[str, Any]:
    """Decode an optional JSON object from Cursor's state database."""
    if value is None:
        return {}
    if isinstance(value, bytes):
        value = value.decode()
    if not isinstance(value, str):
        raise TypeError(f"{key!r} has an unexpected value type")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise TypeError(f"{key!r} is not a JSON object")
    return decoded


def _read_session() -> tuple[str, CursorAccount]:
    """Read the current token and account metadata in one database snapshot."""
    database_uri = f"{CURSOR_STATE_DB.as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=10) as connection:
        rows = dict(
            connection.execute(
                "SELECT key, value FROM ItemTable WHERE key IN (?, ?, ?, ?)",
                (TOKEN_KEY, EMAIL_KEY, PROFILE_KEY, TEAM_KEY),
            ),
        )

    if TOKEN_KEY not in rows:
        raise RuntimeError(f"{TOKEN_KEY!r} was not found in {CURSOR_STATE_DB}")

    token = rows[TOKEN_KEY]
    if isinstance(token, bytes):
        token = token.decode()
    if not isinstance(token, str) or token.count(".") != JWT_SEPARATOR_COUNT:
        raise RuntimeError("Cursor access token has an unexpected format")

    email = rows.get(EMAIL_KEY)
    if isinstance(email, bytes):
        email = email.decode()
    if not isinstance(email, str) or not email.strip():
        raise RuntimeError("Cursor account email is unavailable; sign in again")

    profile = _decode_json_object(rows.get(PROFILE_KEY), PROFILE_KEY)
    team = _decode_json_object(rows.get(TEAM_KEY), TEAM_KEY)
    display_name = profile.get("displayName")
    team_id = team.get("teamId")
    team_name = team.get("name")
    return token, CursorAccount(
        email=email.strip(),
        display_name=display_name.strip()
        if isinstance(display_name, str) and display_name.strip()
        else None,
        team_id=team_id if isinstance(team_id, int) else None,
        team_name=team_name.strip()
        if isinstance(team_name, str) and team_name.strip()
        else None,
    )


def _decode_claims(token: str) -> dict[str, Any]:
    """Decode and validate the claims needed to form Cursor's session cookie."""
    encoded_payload = token.split(".")[1]
    encoded_payload += "=" * (-len(encoded_payload) % 4)
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(encoded_payload),
    )

    if claims.get("type") != "session":
        raise RuntimeError("Cursor token is not a session token")
    if claims.get("aud") != "https://cursor.com":
        raise RuntimeError("Cursor token has an unexpected audience")

    expires_at = claims.get("exp")
    if not isinstance(expires_at, int):
        raise TypeError("Cursor token has no numeric expiry")
    if expires_at <= int(datetime.now(tz=timezone.utc).timestamp()):  # noqa: UP017
        raise RuntimeError("Cursor session token has expired; sign in to Cursor again")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise RuntimeError("Cursor token has no subject")
    return claims


def _push_token(
    token: str,
    subject: str,
    account: CursorAccount,
    webhook_url: str,
) -> None:
    """POST the session cookie value to Home Assistant."""
    if urlsplit(webhook_url).scheme not in {"http", "https"}:
        raise ValueError("Webhook URL must use HTTP or HTTPS")

    cookie_subject = subject.rsplit("|", maxsplit=1)[-1]
    payload = json.dumps(
        {
            "version": PAYLOAD_VERSION,
            "token": f"{cookie_subject}::{token}",
            "account": {
                "subject": subject,
                "email": account.email,
                "display_name": account.display_name,
                "team_id": account.team_id,
                "team_name": account.team_name,
            },
        },
    ).encode()
    request = Request(  # noqa: S310
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310
        if response.status not in {200, 201}:
            raise RuntimeError(
                f"Home Assistant webhook returned HTTP {response.status}",
            )


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the local Cursor token without sending it",
    )
    return parser.parse_args()


def _get_webhook_url() -> str:
    """Return the configured webhook URL."""
    webhook_url = os.environ.get(WEBHOOK_URL_ENV)
    if not webhook_url:
        raise RuntimeError(f"{WEBHOOK_URL_ENV} is not configured")
    return webhook_url


def main() -> int:
    """Validate and optionally push the current Cursor token."""
    args = _parse_args()
    try:
        token, account = _read_session()
        claims = _decode_claims(token)
        if not args.check:
            _push_token(
                token,
                str(claims["sub"]),
                account,
                _get_webhook_url(),
            )
    except (
        HTTPError,
        URLError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"Cursor token push failed: {error}", file=sys.stderr)
        return 1

    expires_at = datetime.fromtimestamp(
        int(claims["exp"]),
        tz=timezone.utc,  # noqa: UP017
    )
    action = "validated" if args.check else "pushed"
    identity = account.email
    if account.display_name:
        identity = f"{account.display_name} <{account.email}>"
    print(
        f"Cursor token for {identity} {action}; expires {expires_at.isoformat()}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
