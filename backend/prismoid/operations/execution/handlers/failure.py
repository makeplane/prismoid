# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An attempt that raised: the device went away, a wall stops every attempt, or the run produced
nothing and the produce ladder takes it from here. The driver keeps the `raise`s, so what reaches
the workflow is the exception the attempt raised.
"""

from __future__ import annotations

from prismoid.ai.frames import PaperFull
from prismoid.errors import ModelIncapable, PolicyDenied
from prismoid.operations.execution import finish, infra, parks
from prismoid.operations.execution.device import NoChrome
from prismoid.operations.execution.handlers.state import Ladder, Ran
from prismoid.operations.execution.record import owe_run
from prismoid.schemas.park import OwedRun


async def device_away(ladder: Ladder, resume: dict[str, object] | None) -> str:
    """The helper stayed away past the grace: the step waits for its device instead of burning infra rungs,
    keeping what it was owed."""
    return await parks.park_device_away(
        ladder.session,
        ladder.task,
        ladder.step,
        why="away",
        seat=ladder.seated,
        resume=resume,
        gotchas=ladder.gotchas,
    )


async def cut_short(ladder: Ladder) -> str:
    """The device cut the attempt short: the step parks for a person instead of re-running."""
    return await parks.park_cut_short(
        ladder.session,
        ladder.task,
        ladder.step,
        seat=ladder.seated,
        gotchas=ladder.gotchas,
    )


async def no_chrome(ladder: Ladder, denied: NoChrome) -> None:
    """The same wall stops every attempt: no rung is spent. Escalates and commits; the caller
    re-raises."""
    ladder.gotchas.append(f"- {denied}")
    await finish.escalate(ladder.session, ladder.task, ladder.step, ladder.spec, ladder.gotchas)
    await ladder.session.commit()


async def produced_nothing(ladder: Ladder, denied: PolicyDenied | ModelIncapable) -> Ran:
    """**An attempt that produced nothing is a failed attempt, not a failed task.** The agent wrote
    a perfectly good memo and forgot to publish it; or it argued with the brief instead of writing.
    Either is one sentence away from being fixed, and telling it so is exactly what the ladder is
    for.

    **And commit before doing anything else.** The model calls happened. The tools ran. The money
    was *spent*. Letting the exception unwind the transaction rolls back the `usage_events` rows
    along with everything else - so the ledger quietly forgets a bill the provider will not forget,
    and the flight recorder loses the journal for the one attempt anybody would actually want to
    read. **Every** provider call is recorded; a failed step is not an exception to that, it is the
    case that needs it most.

    An INFRA death (a provider that failed, a sandbox that stopped answering, a reaped parked
    frame, a wall-clock stop - `infra.booked`) is not a model failure: bump the infra side so the
    fresh `attempt` the ladder is about to spend nets out of `model_attempts()`, and count it
    against the infra cap. It rides the same commit as the usage rows."""
    step = ladder.step
    death = infra.booked(denied)
    if death.infra:
        step.infra_attempts += 1
    if death.counted:
        step.infra_failures += 1
    ladder.backoff = death.backoff
    ladder.final = bool(denied.context.get("final"))  # an unreachable site: no attempt can help
    ladder.no_machine = bool(denied.context.get("machine"))
    ladder.outgrew_paper = ladder.outgrew_paper or isinstance(denied, PaperFull)  # never paper again
    # How the next rung takes up a guarded run that died: a newer death replaces what was owed. A
    # failure with no run of its own (no Machine, a stop before the run) leaves it owed.
    if isinstance(denied, ModelIncapable) and denied.pickup is not None:
        owe_run(step, OwedRun(pickup=denied.pickup, reason=str(denied)))
    kind = denied.kind if isinstance(denied, ModelIncapable) else None
    if kind is not None:
        ladder.failures.append(kind)
    await ladder.session.commit()
    return Ran("", [], None, undelivered=str(denied), failure=kind)
