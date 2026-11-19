# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The push jobs (ADR-100) - an inbox item's alert, `push-inbox-{item}`, and a read's silent
update, `push-read-{item}` / `push-read-all-{actor}-{workspace}-{at}`, on the push queue.

One journaled effect each: the FCM round trips and the prune of dead tokens happen inside it, so a
replayed job returns the recorded count instead of pushing twice. A push never fails anything; the
effect swallows its errors with a log.
"""

from __future__ import annotations

from prismoid.jobs import durable, effects


@durable.job("push_inbox")
async def push_inbox(item_id: str) -> int:
    return await effects.send_inbox_push(item_id)


@durable.job("push_read")
async def push_read(actor_id: str, workspace_id: str, inbox_ids: str) -> int:
    return await effects.send_read_push(actor_id, workspace_id, inbox_ids)
