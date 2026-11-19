# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a stream ticket admits to: the one subject whose event stream it opens, and how long it lives.

The ticket row, the operation that mints and redeems it, and the stream door all speak these words,
so the vocabulary lives here once."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# Seconds between minting a ticket and the last moment it can open a stream. The browser mints
# right before it opens, so a few seconds would do; thirty covers a slow network and a clock-free
# retry, and a ticket is spent by its first use anyway.
STREAM_TICKET_TTL_S: Final = 30


class StreamSubject(StrEnum):
    """The kind of stream a ticket opens: a task's events or a conversation's messages."""

    TASK = "task"
    CONVERSATION = "conversation"
