# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Firebase Cloud Messaging, HTTP v1 (ADR-100): the one place a push leaves Prismoid.

A sibling of `mailer.py` in the layer contract: an infrastructure adapter `operations/push.py`
composes and nothing below it imports. It speaks FCM's wire and nothing else: `render` turns a
`PushMessage` into one token's request body (Android's notification block, APNs' headers and
`aps`), `outcome` reads FCM's answer as sent, dead (the token will never work again: prune it) or
failed (a fault that says nothing about the token).

The access token is the service account's own OAuth token (`google-auth`, already locked through
`google-genai`), minted for the messaging scope and refreshed in a worker thread when it lapses.
The firebase-admin SDK was rejected: it would bring Firestore, Cloud Storage and the API client
library for one POST (ADR-100). A registration token is never logged.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from functools import cache, partial
from typing import Any, Protocol, cast

import httpx
import logfire
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from prismoid.conf import settings
from prismoid.schemas.push import PushChannel, PushMessage, SendOutcome

# FCM's answer that means the token is gone for good: the app was uninstalled or the token rotated.
# SENDER_ID_MISMATCH is not here: it says the KEY is wrong for the token (a wrong or rotated service
# account), and every phone would answer it at once; the device stays and the app re-registers anyway.
_DEAD_CODES = frozenset({"UNREGISTERED"})
_FCM_ERROR = "type.googleapis.com/google.firebase.fcm.v1.FcmError"


class Sender(Protocol):
    """What `operations` may know about the push transport: one message, many tokens, one outcome
    per token in the same order. A failing token never stops the others."""

    async def send_all(self, tokens: Sequence[str], message: PushMessage) -> list[SendOutcome]: ...


def render(token: str, message: PushMessage) -> dict[str, Any]:
    """One token's FCM v1 request body. An alert carries `notification`, Android's channel and tag,
    and APNs' sound and thread id; a silent push carries only data and the badge, as a background
    push (`content-available`, priority 5) on iOS and a normal-priority data message on Android."""
    loud = not message.silent and message.channel is PushChannel.NEEDS_YOU
    android: dict[str, Any] = {"priority": "high" if loud else "normal"}
    aps: dict[str, Any] = {"badge": message.badge}
    body: dict[str, Any] = {"token": token, "data": message.data, "android": android}
    if message.silent:
        aps["content-available"] = 1
        headers = {"apns-push-type": "background", "apns-priority": "5"}
    else:
        body["notification"] = {"title": message.title or "", "body": message.body or ""}
        note = {"channel_id": (message.channel or PushChannel.UPDATES).value}
        if message.tag:
            note["tag"] = message.tag
        android["notification"] = note
        aps["sound"] = "default"
        if message.thread_id:
            aps["thread-id"] = message.thread_id
        headers = {"apns-push-type": "alert", "apns-priority": "10"}
    body["apns"] = {"headers": headers, "payload": {"aps": aps}}
    return {"message": body}


def outcome(status: int, error: dict[str, Any]) -> SendOutcome:
    """FCM's answer for one token. Only an answer about the TOKEN is dead: a 404 or a 400 for any
    other reason (a wrong project in the URL, a malformed payload) is a failure, or one bad deploy
    would prune every device."""
    if 200 <= status < 300:
        return SendOutcome.SENT
    codes = {
        str(detail.get("errorCode", ""))
        for detail in cast(list[dict[str, Any]], error.get("details") or [])
        if detail.get("@type") == _FCM_ERROR
    }
    if codes & _DEAD_CODES:
        return SendOutcome.DEAD
    if "INVALID_ARGUMENT" in codes and "registration token" in str(error.get("message", "")).lower():
        return SendOutcome.DEAD
    return SendOutcome.FAILED


class FcmSender:
    """The FCM HTTP v1 sender for one service account."""

    def __init__(
        self,
        account: dict[str, Any],
        *,
        url: str,
        scope: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
        credentials: Any = None,
    ) -> None:
        # None: the service account's own; a test hands in credentials that need no token endpoint
        self._credentials = credentials or service_account.Credentials.from_service_account_info(
            account, scopes=[scope]
        )
        self._url = url.format(project_id=account["project_id"])
        self._timeout_s = timeout_s
        self._transport = transport  # None: the network; a test hands in a mock transport
        self._refreshing: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None

    async def _bearer(self) -> str:
        """A live access token, refreshed when it lapses (hourly). One refresh at a time: the jobs
        that find it lapsed wait on an asyncio lock, not in executor threads, so a hung token
        endpoint holds one thread from the loop's shared pool, and for at most `timeout_s`."""
        if not self._credentials.valid:
            async with self._refresh_lock():
                if not self._credentials.valid:  # refreshed while this job waited
                    await asyncio.to_thread(self._refresh)
        return str(self._credentials.token)

    def _refresh_lock(self) -> asyncio.Lock:
        """The refresh lock of the running loop. An asyncio lock belongs to one loop; the sender is
        built once per process, so a second loop (a test's, a second worker loop) gets its own."""
        loop = asyncio.get_running_loop()
        if self._refreshing is None or self._refreshing[0] is not loop:
            self._refreshing = (loop, asyncio.Lock())
        return self._refreshing[1]

    def _refresh(self) -> None:
        """Mint a new access token. Blocking, so it runs in a worker thread, bounded by `timeout_s`
        (google-auth's own default is 120 s)."""
        self._credentials.refresh(partial(Request(), timeout=math.ceil(self._timeout_s)))

    async def send_all(self, tokens: Sequence[str], message: PushMessage) -> list[SendOutcome]:
        try:
            bearer = await self._bearer()
        except Exception as exc:
            logfire.error("push: the FCM access token could not be minted: {err}", err=str(exc))
            return [SendOutcome.FAILED] * len(tokens)
        headers = {"Authorization": f"Bearer {bearer}"}
        async with httpx.AsyncClient(
            timeout=self._timeout_s, headers=headers, transport=self._transport
        ) as http:
            return list(await asyncio.gather(*(self._send(http, token, message) for token in tokens)))

    async def _send(self, http: httpx.AsyncClient, token: str, message: PushMessage) -> SendOutcome:
        try:
            response = await http.post(self._url, json=render(token, message))
        except httpx.HTTPError as exc:
            logfire.warn("push: FCM unreachable: {err}", err=type(exc).__name__)
            return SendOutcome.FAILED
        if response.is_success:
            return SendOutcome.SENT
        error = _error_of(response)
        verdict = outcome(response.status_code, error)
        if verdict is SendOutcome.FAILED:
            logfire.warn(
                "push: FCM refused a send: {status} {reason}",
                status=response.status_code,
                reason=str(error.get("status", "")),
            )
        return verdict


def _error_of(response: httpx.Response) -> dict[str, Any]:
    try:
        payload: object = response.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    error = cast(dict[str, Any], payload).get("error")
    return cast(dict[str, Any], error) if isinstance(error, dict) else {}


_installed: Sender | None = None


def install(sender: Sender | None) -> None:
    """Put a sender in place of the configured one (the tests' fake); None goes back to settings."""
    global _installed
    _installed = sender


def sender() -> Sender | None:
    """The sender, or None while push is off (no `PRISMOID_PUSH_CREDENTIALS_JSON`)."""
    return _installed if _installed is not None else _configured()


def enabled() -> bool:
    return _installed is not None or settings.push.enabled()


@cache
def _configured() -> Sender | None:
    account = settings.push.service_account()
    if account is None:
        return None
    push = settings.push
    return FcmSender(account, url=push.fcm_url, scope=push.oauth_scope, timeout_s=push.timeout_s)
