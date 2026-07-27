"""Push Cursor's current session token to a Home Assistant webhook."""

from __future__ import annotations

# ruff: noqa: T201
import argparse
import base64
import json
import os
import sqlite3
import sys
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
WEBHOOK_URL_ENV = "CURSOR_USAGE_WEBHOOK_URL"


def _read_token() -> str:
    """Read the current token without modifying Cursor's database."""
    database_uri = f"{CURSOR_STATE_DB.as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=10) as connection:
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (TOKEN_KEY,),
        ).fetchone()

    if row is None:
        raise RuntimeError(f"{TOKEN_KEY!r} was not found in {CURSOR_STATE_DB}")

    token = row[0]
    if isinstance(token, bytes):
        token = token.decode()
    if not isinstance(token, str) or token.count(".") != JWT_SEPARATOR_COUNT:
        raise RuntimeError("Cursor access token has an unexpected format")
    return token


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


def _push_token(token: str, subject: str, webhook_url: str) -> None:
    """POST the session cookie value to Home Assistant."""
    if urlsplit(webhook_url).scheme not in {"http", "https"}:
        raise ValueError("Webhook URL must use HTTP or HTTPS")

    cookie_subject = subject.rsplit("|", maxsplit=1)[-1]
    payload = json.dumps({"token": f"{cookie_subject}::{token}"}).encode()
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
        token = _read_token()
        claims = _decode_claims(token)
        if not args.check:
            _push_token(token, str(claims["sub"]), _get_webhook_url())
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
    print(f"Cursor token {action}; expires {expires_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
