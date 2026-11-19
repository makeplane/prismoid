# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Fence 1 and the typed ending: the two reads of a finished run before anything builds on it.

Fence 1 comes first: a steer that landed after the run's last safe point sends it round again, in
place, before an expansion, a review or done. Then the typed ending: a guarded agent's Outcome says
what DONE means, and a blocked or failed one fails the step now - the same wall or verdict stops the
next attempt - as an unattended Lead's typed failure does.
"""

from __future__ import annotations

from prismoid.errors import CouldNotDo
from prismoid.operations.execution import finish, steers
from prismoid.operations.execution.handlers.state import AGAIN, Ladder, Ran, Round
from prismoid.schemas.guard import STOPS, stop_gotcha, stop_reason
from prismoid.schemas.lead import Fail
from prismoid.schemas.outputs import Outcome


async def fence_one(ladder: Ladder, ran: Ran) -> Round | None:
    """`AGAIN` when the run missed a steer and goes round in place, else None. Only a run that just
    finished and delivered is fenced (an injected review was fenced when it ran)."""
    if ran.undelivered is not None or not ran.attempted:
        return None
    if await steers.fence(ladder.session, ladder.task, ladder.step, ran.produced, ladder.gotchas):
        return AGAIN
    return None


def typed_end(ran: Ran) -> Outcome | None:
    """The run's typed ending, when its agent is guarded."""
    return ran.outline if isinstance(ran.outline, Outcome) else None


async def blocked(ladder: Ladder, ran: Ran) -> None:
    """Blocked, failed (or an unattended Lead's typed failure) is never a retry: escalate, commit
    and raise. Returns only when the run did not stop."""
    if ran.undelivered is None and isinstance(ran.outline, Fail):
        stopped = ran.outline
        await finish.escalate(
            ladder.session, ladder.task, ladder.step, ladder.spec, ladder.gotchas, stopped=stopped
        )
        await ladder.session.commit()
        raise CouldNotDo(f"the run stopped on its own ({stopped.kind.value}): {stopped.reason}")
    ended = typed_end(ran)
    if ran.undelivered is not None or ended is None or ended.status not in STOPS:
        return
    ladder.gotchas.append(stop_gotcha(ended))
    await finish.escalate(ladder.session, ladder.task, ladder.step, ladder.spec, ladder.gotchas, ended=ended)
    await ladder.session.commit()
    raise CouldNotDo(stop_reason(str(ladder.spec.agent), ended))
