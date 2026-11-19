# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The FCM HTTP v1 wire (ADR-100): what one token's request says on Android and on iOS, and how
FCM's answer is read - only an answer about the token prunes it (never one about the key), and one
bad token never stops the others. No network: the sends go through a mock transport."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from prismoid.fcm import FcmSender, outcome, render
from prismoid.schemas.push import PushChannel, PushMessage, SendOutcome

_FCM_ERROR = "type.googleapis.com/google.firebase.fcm.v1.FcmError"


def _alert(channel: PushChannel, *, tag: str | None = "task-1") -> PushMessage:
    return PushMessage(
        data={"type": "inbox", "inbox_id": "i-1", "unread": "3"},
        badge=3,
        title="Report asks: which quarter?",
        body="1. Which quarter?",
        channel=channel,
        tag=tag,
        thread_id=tag or "question",
    )


def _error(code: str, message: str = "", status: str = "") -> dict[str, Any]:
    return {"message": message, "status": status, "details": [{"@type": _FCM_ERROR, "errorCode": code}]}


def test_a_loud_alert_rides_the_needs_you_channel_at_high_priority() -> None:
    body = render("tok", _alert(PushChannel.NEEDS_YOU))["message"]
    assert body["token"] == "tok"
    assert body["notification"] == {"title": "Report asks: which quarter?", "body": "1. Which quarter?"}
    assert body["data"] == {"type": "inbox", "inbox_id": "i-1", "unread": "3"}
    assert body["android"] == {
        "priority": "high",
        "notification": {"channel_id": "needs_you", "tag": "task-1"},
    }
    assert body["apns"] == {
        "headers": {"apns-push-type": "alert", "apns-priority": "10"},
        "payload": {"aps": {"badge": 3, "sound": "default", "thread-id": "task-1"}},
    }


def test_an_update_is_normal_priority_and_a_taskless_one_has_no_tag() -> None:
    body = render("tok", _alert(PushChannel.UPDATES, tag=None))["message"]
    assert body["android"] == {"priority": "normal", "notification": {"channel_id": "updates"}}
    assert body["apns"]["payload"]["aps"]["thread-id"] == "question"


def test_a_silent_push_is_data_only_a_background_push_on_ios_and_normal_on_android() -> None:
    silent = PushMessage(data={"type": "inbox_read", "inbox_ids": "all", "unread": "0"}, badge=0, silent=True)
    body = render("tok", silent)["message"]
    assert "notification" not in body
    assert body["android"] == {"priority": "normal"}
    assert body["apns"] == {
        "headers": {"apns-push-type": "background", "apns-priority": "5"},
        "payload": {"aps": {"badge": 0, "content-available": 1}},
    }


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (200, {}, SendOutcome.SENT),
        (404, _error("UNREGISTERED", status="NOT_FOUND"), SendOutcome.DEAD),
        # a wrong or rotated key answers this for every phone: the key is at fault, the devices stay
        (403, _error("SENDER_ID_MISMATCH", status="PERMISSION_DENIED"), SendOutcome.FAILED),
        (
            400,
            _error("INVALID_ARGUMENT", "The registration token is not a valid FCM registration token"),
            SendOutcome.DEAD,
        ),
        # a 400 about the payload, or a 404 that names no token, says nothing about the device
        (400, _error("INVALID_ARGUMENT", "Invalid value at 'message.data'"), SendOutcome.FAILED),
        (404, {"message": "Requested entity was not found.", "status": "NOT_FOUND"}, SendOutcome.FAILED),
        (429, _error("QUOTA_EXCEEDED"), SendOutcome.FAILED),
        (503, _error("UNAVAILABLE"), SendOutcome.FAILED),
    ],
)
def test_only_an_answer_about_the_token_is_dead(
    status: int, error: dict[str, Any], expected: SendOutcome
) -> None:
    assert outcome(status, error) is expected


def _account() -> dict[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {
        "type": "service_account",
        "project_id": "proj-1",
        "client_email": "push@proj-1.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.example.invalid/token",
    }


async def test_one_failing_token_never_stops_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["message"]
        seen.append(
            {"url": str(request.url), "auth": request.headers["Authorization"], "token": sent["token"]}
        )
        if sent["token"] == "gone":
            return httpx.Response(404, json={"error": _error("UNREGISTERED", status="NOT_FOUND")})
        if sent["token"] == "down":
            raise httpx.ConnectError("no route")
        return httpx.Response(200, json={"name": "projects/proj-1/messages/1"})

    async def bearer(self: FcmSender) -> str:
        return "access-token"  # no OAuth round trip

    monkeypatch.setattr(FcmSender, "_bearer", bearer)
    sender = FcmSender(
        _account(),
        url="https://fcm.example.invalid/v1/projects/{project_id}/messages:send",
        scope="scope",
        timeout_s=5,
        transport=httpx.MockTransport(answer),
    )
    outcomes = await sender.send_all(["ok-1", "gone", "down", "ok-2"], _alert(PushChannel.UPDATES))

    assert outcomes == [SendOutcome.SENT, SendOutcome.DEAD, SendOutcome.FAILED, SendOutcome.SENT]
    assert {s["url"] for s in seen} == {"https://fcm.example.invalid/v1/projects/proj-1/messages:send"}
    assert {s["auth"] for s in seen} == {"Bearer access-token"}


class _LapsedCredentials:
    """Credentials whose access token has lapsed: `refresh` takes a while, like a slow token
    endpoint, and records the request it was handed."""

    def __init__(self) -> None:
        self.valid = False
        self.token: str | None = None
        self.refreshes: list[Any] = []
        self.threads: set[int] = set()

    def refresh(self, request: Any) -> None:
        self.refreshes.append(request)
        self.threads.add(threading.get_ident())
        time.sleep(0.05)
        self.valid, self.token = True, "fresh-token"


async def test_a_lapsed_token_is_refreshed_once_and_within_the_send_timeout() -> None:
    """Sixteen jobs find the token lapsed at once (the push queue's concurrency): one refresh runs,
    in one thread, bounded by the sender's timeout rather than google-auth's 120 s; the rest wait
    on the loop, not in executor threads, and every send carries the fresh token."""
    auths: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        auths.append(request.headers["Authorization"])
        return httpx.Response(200, json={"name": "projects/proj-1/messages/1"})

    credentials = _LapsedCredentials()
    sender = FcmSender(
        _account(),
        url="https://fcm.example.invalid/v1/projects/{project_id}/messages:send",
        scope="scope",
        timeout_s=7,
        transport=httpx.MockTransport(answer),
        credentials=credentials,
    )
    message = _alert(PushChannel.UPDATES)

    outcomes = await asyncio.gather(*(sender.send_all([f"t{i}"], message) for i in range(16)))

    assert outcomes == [[SendOutcome.SENT]] * 16
    assert auths == ["Bearer fresh-token"] * 16
    assert len(credentials.refreshes) == 1
    assert credentials.refreshes[0].keywords == {"timeout": 7}
    assert len(credentials.threads) == 1
