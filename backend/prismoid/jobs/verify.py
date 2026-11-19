# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`verify_artifact` - the receipt, AFTER the answer (ADR-019).

Verification used to sit between "the agent published" and "the user sees it": a fresh frame,
a suite, a cross-family judge - minutes of wall clock and a whole class of infrastructure
failures - all blocking the deliverable it existed to describe. Now the step completes the
moment the artifact exists, the task delivers, and THIS job signs the receipt afterwards: same
suite, same judge, same signature, same `verification.signed` frame on the same stream (the
web keeps its socket open on a settled task, so the verdict lands live in the thread).

The contract is `remember_task`'s: **a receipt that cannot be signed costs the receipt, never
the thread.** Every failure is swallowed into a log line; the artifact simply stays unverified
(`verdict: null` - "nobody has looked"), which every surface already renders honestly as
Pending. A full host is not one of those failures: it waits `verify_wait_s`, then signs UNVERIFIED
saying no computer was free, because a Pending with no job behind it never ends. Fleet tasks never
come through here - their seats verify in-line by design (the corrective rung is part of the fleet's
contract).
"""

from __future__ import annotations

from uuid import UUID

import logfire

from prismoid.conf import settings
from prismoid.jobs import durable


@durable.effect()
async def _sign_receipt(artifact_id: str) -> str:
    from prismoid.db.models import tenant
    from prismoid.db.models.artifact import Artifact
    from prismoid.db.models.task import Task
    from prismoid.db.models.verification import Verification
    from prismoid.db.session import session_scope
    from prismoid.operations import verification as verification_ops

    try:
        async with session_scope() as session:
            artifact = await session.get(Artifact, UUID(artifact_id))
            if artifact is None:
                return "gone"  # deleted before the receipt ran - nothing to describe
            # The receipt acts for the artifact's task's workspace.
            workspace_id = await tenant.workspace_of_row(session, Task, artifact.task_id)
            if workspace_id is None:
                return "gone"  # the task went with its artifact between the two reads
            tenant.bind(session, workspace_id)
            if artifact.role == "superseded":
                # A steer overtook it before its receipt ran: nobody receives it, so
                # nothing is signed and nothing is spent on it.
                return "superseded"
            if artifact.role != "deliverable":
                # The lane's machinery: sections/outlines/figures never receipt. The
                # enqueue filters too - this is the belt for a replayed or hand-typed job id.
                return "skipped-working"
            if await Verification.latest_for(session, artifact.id) is not None:
                return "already"  # a replay, or a human already triggered one - one receipt
            await verification_ops.verify_artifact(
                session, artifact, frame_wait_s=settings.machines.verify_wait_s
            )
            await session.commit()
            return "signed"
    except Exception:
        logfire.exception(
            "verification failed for artifact {artifact_id}; the verdict stays absent (Pending)",
            artifact_id=artifact_id,
        )
        return "unrun"


@durable.job("verify_artifact")
async def verify_artifact(artifact_id: str) -> str:
    """One artifact, one receipt. Deterministic id `verify-{artifact_id}` - a double enqueue
    dedupes, and the step's existing-row guard absorbs a crash-replay without re-billing."""
    return await _sign_receipt(artifact_id)
