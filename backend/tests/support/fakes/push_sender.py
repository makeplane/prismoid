# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A push transport with no network: it keeps the `fcm.Sender` contract (one outcome per token, in
order) and records every message with the tokens it went to. A token's outcome is scripted by
`answers`; any other token is SENT."""

from __future__ import annotations

from collections.abc import Sequence

from prismoid.schemas.push import PushMessage, SendOutcome


class FakePushSender:
    def __init__(self, answers: dict[str, SendOutcome] | None = None) -> None:
        self.answers = answers or {}
        self.sent: list[tuple[list[str], PushMessage]] = []

    async def send_all(self, tokens: Sequence[str], message: PushMessage) -> list[SendOutcome]:
        self.sent.append((list(tokens), message))
        return [self.answers.get(token, SendOutcome.SENT) for token in tokens]
