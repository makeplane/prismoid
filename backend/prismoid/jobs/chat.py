# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`reply_chat` - one bot reply in one conversation (ADR-057).

The workflow id is `chat-{conversation}-{seq}`: the message that asked. Replies serialize per
conversation on a row claim; a job that finds the claim held does not give up (a DBOS id is
run once - returning early would leave the message unanswered forever) and does not spin: it
sleeps on the durable timer and asks again, until either its message has been answered by
the holder (which batches every unanswered line) or the claim is free. The claim is a LEASE
(see ADR-059): the job waits PAST the lease, so a dead holder's claim expires under it and the
attempt after that claims and answers - the waiting job answers its own message. The model
call sits inside ONE journaled step (`reply_chat_once`), so a worker restart replays a
finished reply from the journal instead of billing it twice.
"""

from __future__ import annotations

import logfire

from prismoid.jobs import durable, effects

_WAIT_S = 2.0
_PAST_THE_LEASE = 10  # extra polls after the lease could have expired


@durable.job("reply_chat")
async def reply_chat(conversation_id: str, seq: int) -> str:
    from prismoid.operations.chat import effective_lease_s

    for _ in range(int(effective_lease_s() / _WAIT_S) + _PAST_THE_LEASE):
        outcome = await effects.reply_chat_once(conversation_id, seq)
        if outcome != "busy":
            return outcome
        await durable.sleep(_WAIT_S)
    logfire.warning(
        "reply_chat {id}#{seq}: the lane stayed claimed past its lease", id=conversation_id, seq=seq
    )
    await effects.reply_chat_stalled(conversation_id, seq)
    return "stalled"
