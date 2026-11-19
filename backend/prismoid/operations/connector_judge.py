# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The pure decisions beside `operations.connectors`, its one caller: values in, a verdict out, no
session. Kept apart so the I/O shell stays a shell."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.connectors import ConnectorToolInfo, ToolPolicy, effective_policies


def foreign_bot(bots: Sequence[ResolvedBot], creator: UUID) -> str | None:
    """Why a step borrows its task creator's connectors because of who wrote its bot, or None.
    A step agent's system prompt is its bot's instructions, so a bot another member wrote steers
    her connectors with that member's words - the borrowing a teammate's steer is
    (`operations.connectors.borrowed_turn`): every mounted tool asks her first, and her silence
    is a deny (the approve gate's timeout). Her own bots keep the dial."""
    for bot in bots:
        if bot.owner_actor_id != creator:
            return f"this step runs {bot.name}, a bot another member wrote"
    return None


def mount_policy(
    overrides: dict[str, Any],
    tools: tuple[ConnectorToolInfo, ...],
    *,
    auto_ask: bool,
    borrowed: str | None,
) -> dict[str, ToolPolicy]:
    """The policy cut for one mount: the effective allow/ask/off per tool. `off` tools never enter
    the spec - for the engine they do not exist; the ask half becomes ApprovalRequired at call
    time (ai/tools/connectors.py)."""
    policy = effective_policies(overrides, tools)
    # **Auto-attached ⇒ writes ask**: a connector the goal's words mounted (no
    # explicit Connect on this task) gates its non-read-only tools on approval regardless
    # of the ALLOW dial - unless the OWNER set an explicit override, which still wins. The
    # "connecting was the consent" rule earns ALLOW only for a service the user clicked.
    if auto_ask:
        for t in tools:
            if t.name not in overrides and policy[t.name] is ToolPolicy.ALLOW and t.read_only is not True:
                policy[t.name] = ToolPolicy.ASK
    if borrowed:
        policy = {n: p if p is ToolPolicy.OFF else ToolPolicy.ASK for n, p in policy.items()}
    return policy


def mount_note(slug: str, mounted: int, off_count: int, borrowed: str | None) -> str:
    """The mount decision on the record (the notes are the engine_fact lines every member
    reads): how many tools mounted, how many policy switched off, and why a read the dial would
    allow is waiting on someone."""
    return (
        f"Mounted connector {slug} - {mounted} tool{'' if mounted == 1 else 's'}"
        + (f" ({off_count} off by policy)" if off_count else "")
        + (
            f" - every call asks the task's creator first: it is their connection, and {borrowed}"
            if borrowed
            else ""
        )
    )
