# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""healthz - the one route with no Actor, and the one compose waits on.

Two disclosures, one shape. Bare `/healthz` answers the only question a load balancer can act
on - ok or degraded - and nothing else; the migration revision and worker/queue vitals it used
to volunteer are reconnaissance on a public box, so they moved to `/healthz/full` behind an
Actor (the split the old comment here promised "before any public exposure"). The probes live
in `operations.health` (no raw SQL or branching in a view); both views stay thin
call-and-serialize.
"""

from __future__ import annotations

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.health import HealthChecksOut, HealthOut
from prismoid.conf import settings
from prismoid.operations import health as ops
from prismoid.operations.device_grants import new_tasks_on_device


async def healthz(session: SessionDep) -> HealthOut:
    # Unauthenticated by design (compose healthchecks, LBs, and the queued-task notice probe it).
    ok = await ops.probe_bare(session)
    return HealthOut(
        status="ok" if ok else "degraded", environment=settings.environment, device=new_tasks_on_device()
    )


async def healthz_full(actor: ActorDep, session: SessionDep) -> HealthOut:
    # The operator's view: any Actor may read it - the vitals are viewer-grade facts, and the
    # web's worker-liveness probe rides a plain session token.
    report = await ops.probe(session)
    return HealthOut(
        status="ok" if report.ok else "degraded",
        environment=settings.environment,
        device=new_tasks_on_device(),
        checks=HealthChecksOut(
            version=settings.worker.app_version,
            database=report.database,
            migration=report.migration,
            worker=report.worker,
            queues=report.queues,
        ),
    )
