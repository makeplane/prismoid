# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Health - the one unauthenticated probe. Typed so the UI can read worker liveness
without casting a free-form object. One shape, two disclosures: bare `/healthz` ships it with
`checks=None` (a stranger learns ok/degraded and nothing else); `/healthz/full` fills it in for
an Actor."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthChecksOut(BaseModel):
    # The deployed release: the git sha deploy.sh pinned (PRISMOID_WORKER_APP_VERSION).
    # "Is my commit live?" is one authed curl compared against `git rev-parse`;
    # it rides /full, not bare, because an exact code version is reconnaissance too.
    version: str = Field(default="dev", description="Deployed release: the pinned git sha, or 'dev'")
    database: str
    migration: str
    # "ok" | "never" | "unknown" | "stale Ns" - informational; never flips top-level status.
    worker: str = Field(description="Worker pulse: ok | never | unknown | stale <n>s")
    # The queues' vital signs from the pulse: "ok" | "CLOGGED: <names>" |
    # "ok (N version ghost(s) pending sweep)" | "unknown". A frozen queue is now a field,
    # not a forensic SQL session. Informational, like the worker line.
    queues: str = Field(default="unknown", description="Queue vitals: ok | CLOGGED: <names> | unknown")


class HealthOut(BaseModel):
    status: str  # "ok" | "degraded"
    environment: str
    # A task created here runs on this computer: the CLI's `--folder`
    # defaults to where it runs only then, and never names a folder of the person's to a cloud.
    device: bool = False
    # None on the bare probe - the migration revision and worker/queue vitals are an operator's
    # facts, not a load balancer's (they ride /healthz/full, behind an Actor).
    checks: HealthChecksOut | None = None
