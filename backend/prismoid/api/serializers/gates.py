# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Gates - request and response bodies. `*In` is a request, `*Out` is a response.

The QuestionSet crosses this boundary **whole and already numbered**. Every surface - the CLI, an
HTTP client, an MCP host - renders the same list in the same order, so that `1: b, 2: yes` means one
thing everywhere. A client that had to number the questions itself would be a second place for the
numbering to be wrong.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.gate import Gate
from prismoid.schemas.gate_view import viewer_reason
from prismoid.schemas.machines import FrameDriverName
from prismoid.schemas.plan import BudgetFigures, GateAction, GateResolution, GateSpec, QuestionSet


class GateResolveIn(BaseModel):
    action: GateAction = GateAction.ACCEPT
    content: dict[str, Any] | None = Field(
        default=None,
        description=(
            "answers by question id (q1, q2, …). On decline, the answers given stand and every other "
            "question takes its default. Ignored for cancel."
        ),
    )


class GateOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    task_id: UUID
    kind: str
    title: str
    questions: QuestionSet | None = None
    # The gate's evidence: an approve gate's per-call blocks, a connect gate's why.
    reason: str = ""
    # CONNECT gates: which service the block offers.
    connector_slug: str = ""
    # APPROVE gates only: where the ask came from ("connector" | "browser"), the host being
    # asked about, and the scopes the human may choose (empty for a connector gate or a
    # browser danger ask).
    source: str = "connector"
    host: str = ""
    scopes: list[str] = Field(default_factory=list)
    # APPROVE gates only: why the approval rules asked, each once - the card says it first.
    asks_because: list[str] = Field(default_factory=list)
    # BUDGET gates only (B3): which ceiling "Budget reached" hit - "task" or "project" - so the
    # dock offers the raise only where the viewer can make it. Empty for the cost confirmation.
    budget_scope: str = ""
    # BUDGET gates only: the ceiling, its spend and the coming wave's estimate - what a raise that
    # resumes must clear. Null for the cost confirmation and older gates.
    budget_figures: BudgetFigures | None = None
    expires_at: dt.datetime | None = None
    created_at: dt.datetime
    # `device` when the task's browser is a window on the user's own computer:
    # its CONTROL gate is ended with Done, the creator's accept. Null on a cloud Machine,
    # whose driver is never named.
    machine_driver: FrameDriverName | None = None


class GateResolvedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate_id: UUID
    action: GateAction
    content: dict[str, str | int | float | bool | list[str]] | None = None
    resolved_by: str = ""
    # The resolver as a user id (None for the clock, or a resolution older than the field): the
    # web names "who answered first" by id, since two people can share a name.
    resolved_by_id: UUID | None = None
    at: dt.datetime


def gate_out(
    gate: Gate, *, viewer_id: UUID, creator_actor_id: UUID, machine_driver: str | None = None
) -> GateOut:
    """`always` is offered to the task's CREATOR only (a standing allowance binds
    every future task of theirs, so an editor may accept `once`/`task` but never `always` -
    `resolve` 403s it). The viewer is never shown a choice the resolve would refuse. A device-away
    gate's cut-off command and a connector approval's arguments are the creator's to read
    (`gate_view.viewer_reason`)."""
    spec = GateSpec.model_validate(gate.spec)
    creator = viewer_id == creator_actor_id
    scopes = list(spec.scopes) if creator else [s for s in spec.scopes if s != "always"]
    return GateOut(
        id=gate.id,
        task_id=gate.task_id,
        kind=spec.kind.value,
        title=spec.title,
        questions=spec.questions,
        reason=spec.reason if creator else viewer_reason(spec.reason, spec.kind, spec.source),
        connector_slug=spec.connector_slug,
        source=spec.source,
        host=spec.host,
        scopes=scopes,
        asks_because=list(spec.asks_because),
        budget_scope=spec.budget_scope,
        budget_figures=spec.budget_figures,
        expires_at=gate.expires_at,
        created_at=gate.created_at,
        machine_driver=FrameDriverName(machine_driver) if machine_driver else None,
    )


def gate_resolved(gate_id: UUID, resolution: GateResolution) -> GateResolvedOut:
    return GateResolvedOut(
        gate_id=gate_id,
        action=resolution.action,
        content=resolution.content,
        resolved_by=resolution.resolved_by,
        resolved_by_id=resolution.resolved_by_id,
        at=resolution.at,
    )
