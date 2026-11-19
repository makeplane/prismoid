# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Push notifications to a person's phone: the words a device row, the device door and the push
sender share, and the one message value the inbox hands the FCM adapter (`prismoid/fcm.py`).

The message is transport-neutral on purpose: the inbox decides what a phone should show and hear,
the adapter decides how FCM's HTTP v1 wire says it for Android and for APNs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PushDevicePlatform(StrEnum):
    """The phone a registration token belongs to."""

    IOS = "ios"
    ANDROID = "android"


class PushChannel(StrEnum):
    """The Android notification channel an alert rides; the app creates both. `needs_you` is loud
    (a gate, a question, a budget stop: the work waits on the person), `updates` is the rest."""

    NEEDS_YOU = "needs_you"
    UPDATES = "updates"


class SendOutcome(StrEnum):
    """What FCM said about one token: delivered, gone for good (the row is pruned), or a failure
    that says nothing about the token (logged, the row stays)."""

    SENT = "sent"
    DEAD = "dead"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PushMessage:
    """One push, before any token. `data` values are strings (FCM's rule). An alert carries a
    `title`/`body` and a `channel`; a silent one (`silent=True`) carries neither and only moves the
    badge and the app's state. `tag` makes a newer Android alert replace an older one with the same
    tag; `thread_id` groups alerts on iOS."""

    data: dict[str, str]
    badge: int
    title: str | None = None
    body: str | None = None
    channel: PushChannel | None = None
    tag: str | None = None
    thread_id: str | None = None
    silent: bool = False


@dataclass(frozen=True, slots=True)
class PushDeviceRegistration:
    """A phone asking to be pushed to: its FCM registration token and the app build that holds it."""

    token: str
    platform: PushDevicePlatform
    app_id: str
    app_version: str
