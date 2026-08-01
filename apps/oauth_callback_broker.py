"""Securely broker OAuth callbacks to AppDaemon apps."""

from __future__ import annotations

import asyncio
import html
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from enum import StrEnum
from json import dumps
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from urllib.parse import urlencode

from aiohttp import web
from appdaemon.plugins.hass.hassapi import Hass
from wg_utilities.clients.oauth_client import OAuthClient, OAuthCredentials

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

CALLBACK_URI: Final[str] = "https://oauth.worgarsi.de/app/oauth-callback"
PENDING_FLOW_TTL_SECONDS: Final[int] = 15 * 60
PENDING_FLOW_DB: Final[Path] = Path(
    "/data/oauth_callback/pending_flows.sqlite3",
)

SECURITY_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store, max-age=0",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class OAuthProvider(StrEnum):
    """OAuth providers supported by the callback broker."""

    MONZO = "monzo"
    SPOTIFY = "spotify"
    TRUELAYER = "truelayer"


class PendingFlowError(ValueError):
    """Raised when a callback state cannot be consumed."""


@dataclass(frozen=True, slots=True)
class PendingFlow:
    """A pending OAuth flow and its dispatch destination."""

    target_app: str
    flow_ref: str
    provider: OAuthProvider
    created_at: float
    expires_at: float


class OAuthConsumer(Protocol):
    """Interface implemented by apps which receive OAuth callbacks."""

    def complete_oauth_authorization(
        self,
        flow_ref: str,
        code: str,
        redirect_uri: str,
    ) -> None:
        """Exchange and persist an authorization code."""

    def oauth_authorization_failed(self, flow_ref: str) -> None:
        """Start a fresh authorization flow after an exchange failure."""


@dataclass(frozen=True, slots=True)
class OAuthRetryPolicy:
    """Retry configuration for providers requiring approval after code exchange."""

    interval_seconds: int = 30
    max_attempts: int = 20


@dataclass(frozen=True, slots=True)
class OAuthFlow:
    """Everything needed to authorize one client owned by an AppDaemon app."""

    ref: str
    provider: OAuthProvider
    client: OAuthClient[Any]
    reauth_var: str
    notification_id: str
    notification_title: str
    notification_message: str
    on_authorized: Callable[[], bool | None]
    trigger_entity: str
    notify_script: str = "script.notify_will"
    auth_params: Mapping[str, str] = field(default_factory=dict)
    retry_policy: OAuthRetryPolicy | None = None


class OAuthFlowManager:
    """Manage authorization lifecycle details shared by OAuth consumer apps."""

    def __init__(
        self,
        app: Hass,
        broker: OAuthCallbackBroker,
        flows: list[OAuthFlow],
    ) -> None:
        self.app = app
        self.broker = broker
        self.flows = {flow.ref: flow for flow in flows}
        if len(self.flows) != len(flows):
            raise ValueError("OAuth flow refs must be unique within an app")
        self._flow_ref_by_trigger = {
            flow.trigger_entity: flow.ref for flow in self.flows.values()
        }
        if len(self._flow_ref_by_trigger) != len(flows):
            raise ValueError("OAuth trigger entities must be unique within an app")
        self._retry_attempts: dict[str, int] = {}
        self.app.listen_state(
            self._manual_trigger_callback,
            list(self._flow_ref_by_trigger),
        )

    def _manual_trigger_callback(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        """Start a fresh flow whenever its Home Assistant button is pressed."""
        del attribute, old, new, kwargs
        self.start(self._flow_ref_by_trigger[entity])

    def _get_flow(self, flow_ref: str) -> OAuthFlow:
        try:
            return self.flows[flow_ref]
        except KeyError:
            raise ValueError(f"Unknown OAuth flow {flow_ref!r}") from None

    def start(self, flow_ref: str) -> None:
        """Create a pending flow and publish its authorization link."""
        flow = self._get_flow(flow_ref)
        auth_link = self.broker.begin_authorization(
            self.app.name,
            flow.ref,
            flow.provider,
            flow.client,
            flow.auth_params,
        )
        self._set_reauth(flow, needs_reauth=True, auth_link=auth_link)
        self.app.call_service(
            "script/turn_on",
            entity_id=flow.notify_script,
            variables={
                "clear_notification": True,
                "title": flow.notification_title,
                "message": flow.notification_message,
                "notification_id": flow.notification_id,
                "mobile_notification_icon": "mdi:key-alert-outline",
                "actions": dumps(
                    [{"action": "URI", "title": "Auth Link", "uri": auth_link}],
                ),
            },
        )

    def complete(self, flow_ref: str, code: str, redirect_uri: str) -> None:
        """Exchange a code and run or schedule the consumer's initialization."""
        flow = self._get_flow(flow_ref)
        exchange_authorization_code(
            client=flow.client,
            provider=flow.provider,
            code=code,
            redirect_uri=redirect_uri,
        )

        if flow.retry_policy is None:
            result = flow.on_authorized()
            if result is False:
                raise RuntimeError(
                    f"OAuth flow {flow_ref!r} remained unavailable after authorization",
                )
            return

        self._retry_attempts[flow_ref] = 0
        self.app.run_in(self._retry_authorized_callback, 0, flow_ref=flow_ref)

    def _retry_authorized_callback(self, kwargs: dict[str, Any]) -> None:
        """Retry provider initialization until approval becomes usable."""
        flow_ref = str(kwargs["flow_ref"])
        flow = self._get_flow(flow_ref)
        retry_policy = flow.retry_policy
        if retry_policy is None:
            raise RuntimeError(f"OAuth flow {flow_ref!r} has no retry policy")

        if flow.on_authorized() is not False:
            self._retry_attempts.pop(flow_ref, None)
            return

        attempts = self._retry_attempts.get(flow_ref, 0) + 1
        self._retry_attempts[flow_ref] = attempts
        if attempts >= retry_policy.max_attempts:
            self.app.error(
                "OAuth initialization for %s failed after %i attempts; "
                "leaving reauth state set",
                flow_ref,
                attempts,
            )
            return

        self.app.run_in(
            self._retry_authorized_callback,
            retry_policy.interval_seconds,
            flow_ref=flow_ref,
        )

    def clear(self, flow_ref: str) -> None:
        """Clear the flow's notification and reauth state."""
        flow = self._get_flow(flow_ref)
        self._set_reauth(flow, needs_reauth=False)
        self.app.call_service(
            "script/turn_on",
            entity_id=flow.notify_script,
            variables={
                "clear_notification": True,
                "notification_id": flow.notification_id,
            },
        )

    def _set_reauth(
        self,
        flow: OAuthFlow,
        *,
        needs_reauth: bool,
        auth_link: str = "",
    ) -> None:
        self.app.call_service(
            "var/set",
            entity_id=flow.reauth_var,
            value="on" if needs_reauth else "off",
            force_update=True,
            attributes={"auth_link": auth_link},
        )


class OAuthFlowConsumerMixin:
    """Implement the callback broker protocol through an OAuth flow manager."""

    oauth: OAuthFlowManager

    def complete_oauth_authorization(
        self,
        flow_ref: str,
        code: str,
        redirect_uri: str,
    ) -> None:
        """Delegate code exchange and post-authorization initialization."""
        self.oauth.complete(flow_ref, code, redirect_uri)

    def oauth_authorization_failed(self, flow_ref: str) -> None:
        """Issue a fresh authorization link after a rejected callback."""
        self.oauth.start(flow_ref)


class PendingFlowStore:
    """SQLite-backed, single-use OAuth state storage."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.chmod(0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_flows (
                    state TEXT PRIMARY KEY,
                    target_app TEXT NOT NULL,
                    flow_ref TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    UNIQUE (target_app, flow_ref)
                )
                """,
            )
        self.db_path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def create(
        self,
        *,
        state: str,
        target_app: str,
        flow_ref: str,
        provider: OAuthProvider,
        now: float | None = None,
    ) -> PendingFlow:
        """Create a flow, replacing any earlier flow for the same target."""
        created_at = time.time() if now is None else now
        expires_at = created_at + PENDING_FLOW_TTL_SECONDS

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM pending_flows WHERE target_app = ? AND flow_ref = ?",
                (target_app, flow_ref),
            )
            connection.execute(
                """
                INSERT INTO pending_flows (
                    state, target_app, flow_ref, provider, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    state,
                    target_app,
                    flow_ref,
                    provider.value,
                    created_at,
                    expires_at,
                ),
            )

        return PendingFlow(target_app, flow_ref, provider, created_at, expires_at)

    def consume(self, state: str, *, now: float | None = None) -> PendingFlow:
        """Atomically consume a state, rejecting unknown, expired, and replayed values."""
        consumed_at = time.time() if now is None else now

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT target_app, flow_ref, provider, created_at, expires_at
                FROM pending_flows
                WHERE state = ?
                """,
                (state,),
            ).fetchone()
            if row is not None:
                connection.execute("DELETE FROM pending_flows WHERE state = ?", (state,))
            connection.execute(
                "DELETE FROM pending_flows WHERE expires_at <= ?",
                (consumed_at,),
            )

        if row is None:
            raise PendingFlowError(
                "This authorization link is invalid or was already used.",
            )

        target_app, flow_ref, provider_value, created_at, expires_at = row
        if expires_at <= consumed_at:
            raise PendingFlowError("This authorization link has expired.")

        return PendingFlow(
            target_app=target_app,
            flow_ref=flow_ref,
            provider=OAuthProvider(provider_value),
            created_at=created_at,
            expires_at=expires_at,
        )


def exchange_authorization_code(
    *,
    client: OAuthClient[Any],
    provider: OAuthProvider,
    code: str,
    redirect_uri: str,
) -> None:
    """Exchange a code using the provider's required request encoding."""
    payload = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "redirect_uri": redirect_uri,
    }
    form_encoded = provider in {OAuthProvider.MONZO, OAuthProvider.SPOTIFY}
    credentials: dict[str, Any] = client.post_json_response(
        client.access_token_endpoint,
        data=payload if form_encoded else None,
        json=None if form_encoded else payload,
        header_overrides=(
            {"Content-Type": "application/x-www-form-urlencoded"} if form_encoded else {}
        ),
    )
    credentials["client_id"] = client.client_id
    credentials["client_secret"] = client.client_secret
    client.credentials = OAuthCredentials.parse_first_time_login(credentials)


class OAuthCallbackBroker(Hass):
    """Correlate browser callbacks with the AppDaemon app that started them."""

    store: PendingFlowStore

    def initialize(self) -> None:
        """Register the public callback and clean-result routes."""
        # aiohttp's standard access format includes the query string, which contains
        # authorization codes on this route.
        getLogger("aiohttp.access").disabled = True
        self.store = PendingFlowStore(Path(self.args.get("state_db", PENDING_FLOW_DB)))
        self.redirect_uri = str(self.args.get("redirect_uri", CALLBACK_URI))
        self.register_route(self.oauth_callback, "oauth-callback")
        self.register_route(self.oauth_complete, "oauth-complete")

    def begin_authorization(
        self,
        target_app: str,
        flow_ref: str,
        provider: OAuthProvider,
        client: OAuthClient[Any],
        auth_params: Mapping[str, str] | None = None,
    ) -> str:
        """Persist a pending flow and return its provider authorization URL."""
        state = secrets.token_urlsafe(32)
        self.store.create(
            state=state,
            target_app=target_app,
            flow_ref=flow_ref,
            provider=provider,
        )

        params = dict(auth_params or {})
        params.update(
            {
                "client_id": client.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "state": state,
            },
        )
        if client.scopes and "scope" not in params:
            params["scope"] = " ".join(client.scopes)

        return f"{client.auth_link_base}?{urlencode(params)}"

    async def oauth_callback(
        self,
        request: web.Request,
        _: dict[str, Any],
    ) -> web.Response:
        """Validate and dispatch a provider callback without retaining its code."""
        state = request.query.get("state")
        code = request.query.get("code")
        provider_error = request.query.get("error")

        if not state:
            return self._result_redirect("error", "The callback did not include state.")

        try:
            flow = self.store.consume(state)
        except PendingFlowError as err:
            return self._result_redirect("error", str(err))

        if provider_error or not code:
            await self._restart_flow(flow)
            return self._result_redirect(
                "error",
                "Authorization was cancelled or rejected. A fresh link has been sent.",
            )

        try:
            target = cast(
                "OAuthConsumer",
                await cast("Awaitable[Any]", self.get_app(flow.target_app)),
            )
            await asyncio.to_thread(
                target.complete_oauth_authorization,
                flow.flow_ref,
                code,
                self.redirect_uri,
            )
        except Exception:
            self.error(
                "OAuth exchange failed for app %s flow %s",
                flow.target_app,
                flow.flow_ref,
            )
            await self._restart_flow(flow)
            return self._result_redirect(
                "error",
                "Authorization could not be completed. A fresh link has been sent.",
            )

        return self._result_redirect("success", "Authorization completed successfully.")

    async def _restart_flow(self, flow: PendingFlow) -> None:
        """Ask the target to create a fresh single-use authorization link."""
        try:
            target = cast(
                "OAuthConsumer",
                await cast("Awaitable[Any]", self.get_app(flow.target_app)),
            )
            await asyncio.to_thread(target.oauth_authorization_failed, flow.flow_ref)
        except Exception:
            self.error(
                "Unable to restart OAuth for app %s flow %s",
                flow.target_app,
                flow.flow_ref,
            )

    def _result_redirect(self, status: str, message: str) -> web.Response:
        location = (
            f"/app/oauth-complete?{urlencode({'status': status, 'message': message})}"
        )
        return web.Response(
            status=303,
            headers={**SECURITY_HEADERS, "Location": location},
        )

    async def oauth_complete(
        self,
        request: web.Request,
        _: dict[str, Any],
    ) -> web.Response:
        """Render a code-free result page."""
        success = request.query.get("status") == "success"
        title = "Authorization complete" if success else "Authorization failed"
        default_message = "You can close this window."
        message = request.query.get("message", default_message)
        colour = "#2e7d32" if success else "#b3261e"
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title><style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:34rem;padding:2rem;border-radius:1rem;background:#1e1e1e;text-align:center}}
h1{{color:{colour}}}
</style></head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>
<p>You can close this window.</p></main></body></html>"""
        return web.Response(
            text=body,
            content_type="text/html",
            headers=SECURITY_HEADERS,
        )
