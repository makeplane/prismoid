# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A task's dials, at the door: what a submit or a follow-up may ask for.

A dial that cannot be honoured is a 409 or a 422 at submit, never a planning surprise, and these
checks run before anything is stored, so a refusal leaves no orphaned attachment. A fork inherits
its parent's dials (`inherit_mode`) and skips them: they were checked at the parent's submit, and
planning re-checks eligibility anyway. The size rule alone has a second home, under the follow-up's
row lock, because it reads the thread's mode.

Intent attach: when the user attached nothing, the goal's own words may pick from the owner's live
`auto_attach` connectors (deterministic word-boundary matching, never a model) - only on a fresh,
human-born task, since forks inherit their filter and a routine-minted task has nobody to answer an
approval gate. Its "slug:keyword" pairs are journaled on `task.created`: a credential mounting itself
wears its reason. Every auto-mounted tool still asks unless its owner or the catalog allowed it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.errors import FeatureDisabled, ValidationFailed
from prismoid.operations import allowance, connector_attach
from prismoid.operations import connectors as connector_ops
from prismoid.schemas import sizing
from prismoid.schemas.model_class import EXECUTOR_CLASSES
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import TaskMode
from prismoid.schemas.usage import DEFAULT_CAP_SOURCE, usd_ceiling


async def _validate_fleet_seats(session: AsyncSession, models: Sequence[str]) -> None:
    """Fleet seats resolve against the enabled catalog at the door, exactly like skill
    refs: a bad seat is a 422 at submit, never a planning surprise. Only executor-capable models
    can hold a seat - a judge- or vision-only row cannot author a document."""
    from prismoid.operations import models as model_ops

    catalog = await model_ops.catalog(session, enabled_only=True)
    flyable = {m.ref for m in catalog if any(m.serves(c) for c in EXECUTOR_CLASSES)}
    unknown = sorted(set(models) - flyable)
    if unknown:
        raise ValidationFailed(
            "these are not enabled executor models",
            fields=dict.fromkeys(unknown, "not an enabled executor model"),
        )


def _validate_deliverable(deliverable: str) -> None:
    """The chosen kind is validated against what the roster can actually publish -
    the honest set is derived, not typed: the kinds the plannable agents publish."""
    from prismoid.ai.agents.base import roster as agent_roster

    producible = {kind.value for a in agent_roster(plannable=True) if a.publishes for kind in a.kinds}
    if deliverable not in producible:
        raise ValidationFailed(
            f"no agent publishes {deliverable!r}",
            fields={"deliverable": f"one of: {', '.join(sorted(producible))}"},
        )


def _imply_document(deliverable: str | None) -> str:
    """A file format is a DOCUMENT choice: with any other kind it is a
    422 naming the conflict; alone it implies the Document kind - picking "PDF" IS picking
    a document, and the implication is the user's own dial. One rule, both doors."""
    if deliverable is not None and deliverable != ArtifactType.DOCUMENT.value:
        raise ValidationFailed(
            f"a file format belongs to documents - a {deliverable} is its own format",
            fields={"deliverable_format": "drop it, or choose the Document kind"},
        )
    return ArtifactType.DOCUMENT.value


def refuse_big_fleet(scale: str) -> None:
    """A fleet is its own plan shape - single-breath seats only: the explicit big buckets
    are refused; an `auto` that RESOLVES big is refused at planning by the same predicate."""

    bucket = sizing.normalize(scale)
    if bucket is not None and not sizing.fleet_allows(bucket):
        raise ValidationFailed(
            "fleet answers are single-breath - choose brief or standard (or drop the size)"
        )


def with_default_cap(budget: dict[str, object] | None) -> dict[str, object] | None:
    """A task created without a dollar cap gets the deployment's default (ADR-087 section 1), in the
    stored shape the budget endpoint writes (dollars and cents), marked as the default: nobody chose
    it, so it never stands for consent to a cost. An explicit cap is never touched."""
    default = settings.workspaces.task_default_budget_usd
    if default is None or usd_ceiling(budget) is not None:
        return budget
    return {**(budget or {}), "usd": str(default.quantize(Decimal("0.01"))), "source": DEFAULT_CAP_SOURCE}


async def create_dials(
    session: AsyncSession,
    actor: Actor,
    *,
    project_id: uuid.UUID | None,
    mode: TaskMode,
    models: Sequence[str],
    deliverable: str | None,
    deliverable_format: str | None,
    scale: str,
    inherit_mode: bool,
) -> tuple[TaskMode, str | None]:
    """A new task's door: a workspace that spent its allowance is refused first (ADR-103, after
    `create_task`'s replay guard and before anything is stored), then the dials - the mode it runs
    in (a legacy "council" becomes `fleet`) and the deliverable it asks for (a format alone implies
    the Document kind)."""
    await allowance.require(session, tenant.workspace_of(session), actor=actor)
    # **Fleet mode is behind a flag**. Refused *before* the row and
    # the enqueue, so a `--mode fleet` submitted while the feature is off is an honest 409 at the
    # door, not a task that plans a fleet nobody turned on. The flag is project-scoped, so a project can
    # opt in early. The second enum member is the legacy wire value - accepted, stored as `fleet`.
    # A FORK inherits its parent's mode as thread-DNA (`inherit_mode`) and skips the re-check:
    # the flag gates the *choice*, and the choice was made when the parent was submitted -
    # flipping it later must not orphan a settled thread's branch.
    if mode in (TaskMode.FLEET, TaskMode.COUNCIL):
        mode = TaskMode.FLEET
        if not inherit_mode:
            from prismoid.operations import features

            if not await features.enabled(session, features.FLEET, project_id):
                raise FeatureDisabled("fleet mode is off - enable prismoid:fleet to use --mode fleet")

    # Fleet seats and the chosen deliverable are validated at the door - unless inherited from a
    # parent (fork), where they were validated at the parent's submit and planning re-checks
    # eligibility anyway (a since-disabled seat costs the seat, never the task; a since-retired
    # kind fails its plan honestly).
    if models:
        if mode is not TaskMode.FLEET:
            raise ValidationFailed("models are fleet seats - set mode=fleet to choose them")
        if not inherit_mode:
            await _validate_fleet_seats(session, models)

    # Fleet refuses a deliverable choice: a fleet delivers the chair's comparison table.
    # (A legacy "council" create was normalized to FLEET above, so this guard covers it.)
    if deliverable is not None:
        if mode is TaskMode.FLEET:
            raise ValidationFailed("fleet mode delivers a comparison table - a deliverable cannot be chosen")
        if not inherit_mode:
            _validate_deliverable(deliverable)

    # The format-implies-document rule, shared with `turn_dials`; the
    # implication is the user's own dial ("user" source at the row).
    if deliverable_format is not None and not inherit_mode:
        if mode is TaskMode.FLEET:
            raise ValidationFailed("fleet mode delivers a comparison table - a format cannot be chosen")
        deliverable = _imply_document(deliverable)

    if mode is TaskMode.FLEET:
        refuse_big_fleet(scale)
    return mode, deliverable


async def create_connectors(
    session: AsyncSession, actor: Actor, goal: str, connectors: Sequence[str], *, inherit: bool, routine: bool
) -> tuple[list[str], list[str], list[str]]:
    """A submit's connectors: (row ids, the intent attach's "slug:keyword" pairs, the ids it mounted).

    **Connector refs → resolved row ids, at submit time**, like skill pins. Validated at the door (owner's, live) so "not connected" is a 422 naming the
    fix, never a mid-run surprise. A FORK inherits ids pre-filtered by `fork()` to what still
    mounts AND what the forking actor owns (`inherit_mode` - a dead or foreign connector must
    not orphan a branch; the mount re-checks ownership regardless)."""
    if inherit:
        return list(connectors), [], []
    connector_ids: list[str] = []
    if connectors:
        connector_ids = await connector_ops.validate_refs(session, actor, list(connectors))
    # intent attach (module docstring): explicit attaches win, and a routine has nobody to approve
    if connector_ids or routine:
        return connector_ids, [], []
    auto_ids, connectors_auto = await connector_attach.auto_attach_candidates(session, actor.id, goal)
    connector_ids = auto_ids[: connector_ops.MAX_TASK_CONNECTORS]
    # the mounted ids ride the row as provenance (`task.connectors_auto`)
    return connector_ids, connectors_auto[: len(connector_ids)], list(connector_ids)


def _a_fleet(task: Task) -> bool:
    """COUNCIL is the pre-rename fleet wire value - stored rows still carry it, and every
    fleet-shaped guard must treat the pair as one mode."""
    return TaskMode(task.mode) in (TaskMode.FLEET, TaskMode.COUNCIL)


async def turn_dials(
    session: AsyncSession,
    task: Task,
    *,
    models: Sequence[str],
    deliverable: str | None,
    deliverable_format: str | None,
) -> str | None:
    """A follow-up turn's dials, against the thread's own mode (thread-DNA): the deliverable it asks
    for, a format alone flipping this turn's kind to document, the user's own dial."""
    if models:
        if not _a_fleet(task):
            raise ValidationFailed("models are fleet seats - this thread is not a fleet")
        await _validate_fleet_seats(session, models)
    if deliverable is not None:
        if _a_fleet(task):
            raise ValidationFailed("fleet mode delivers a comparison table - a deliverable cannot be chosen")
        _validate_deliverable(deliverable)
    if deliverable_format is not None:
        if _a_fleet(task):
            raise ValidationFailed("fleet mode delivers a comparison table - a format cannot be chosen")
        deliverable = _imply_document(deliverable)
    return deliverable
