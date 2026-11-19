# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Task - a unit of work a user submits; plans into Steps; delivers signed artifacts.

At most one Project (a single nullable FK - never a join table). May have a parent: a
follow-up inherits the project and mounts the parent's artifacts read-only.

**`schemas/task_states.ALLOWED` IS the task state machine.** Not a copy of it, not an interpretation
of it - the
machine itself. Every transition in the product goes through `transition()`, so a state change the
diagram forbids cannot be reached by any code path, including the ones written next year by
someone who never read the diagram.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ColumnElement,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import (
    Base,
    StrEnumColumn,
    created_at,
    escape_like,
    keyset_before,
    keyset_cursor,
    uuid_pk,
    vocabulary,
)
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.errors import InvalidTransition, NotFound
from prismoid.schemas.machines import FrameDriverName
from prismoid.schemas.plan import TaskStatus
from prismoid.schemas.task_states import ALLOWED, TERMINAL


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        vocabulary("status", TaskStatus),
        # A project is referenced WITH its workspace: a row can never be filed in
        # another workspace's project. Deleting the project unfiles the row (only project_id nulls; workspace_id stays).
        ForeignKeyConstraint(
            ["project_id", "workspace_id"],
            ["projects.id", "projects.workspace_id"],
            name="fk_tasks_project_workspace",
            ondelete="SET NULL (project_id)",
        ),
        Index("ix_tasks_project_id", "project_id"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_parent_task_id", "parent_task_id"),
        Index("ix_tasks_created_at_id", "created_at", "id"),  # keyset pagination, only
        # `creator_actor_id` fronts every visibility query (`_visible_to`) and the idempotency
        # lookup; unindexed it was a seq scan on the hottest read path (audit finding).
        Index("ix_tasks_creator_actor_id", "creator_actor_id"),
        # The duplicate-send guardrail's partial-unique index, declared here so metadata matches
        # migration d7f2a9c4e6b8 exactly (the drift the audit flagged): NULL means "no token
        # offered" and stays unconstrained, so replays collide but bare sends never do. Per workspace
        # (ADR-061): the replay lookup reads the bound workspace, so the key it guards is per workspace too.
        Index(
            "ix_tasks_creator_client_token",
            "workspace_id",
            "creator_actor_id",
            "client_token",
            unique=True,
            postgresql_where=text("client_token IS NOT NULL"),
        ),
        # `live_for_routine` rides this on every non-manual fire's overlap gate; declared here
        # so metadata matches its migration exactly (the same drift rule as the
        # client-token index above - it was migration-only).
        Index("ix_tasks_origin_routine_id", "origin_routine_id"),
        # Its chat-born twin, migration-only since 4b1450af1681 (same drift rule; found by the
        # baseline's schema proof).
        Index(
            "ix_tasks_origin_conversation_id",
            "origin_conversation_id",
            postgresql_where=text("origin_conversation_id IS NOT NULL"),
        ),
        # The target of `machines`' composite (task_id, workspace_id) FK: a Machine is
        # referenced WITH its task's workspace, so it can never belong to another workspace's task.
        UniqueConstraint("id", "workspace_id", name="uq_tasks_id_workspace"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    goal: Mapped[str] = mapped_column(String(8000), nullable=False)
    # What the creator calls it (the sidebar name). NULL = derive from the goal. The goal is
    # immutable - it is what was asked; the title is only a label.
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    # Pinned to the top of the creator's list. User-space triage, NOT a status: the machine's
    # states stay the machine's - a pin never claims anything about the work.
    pinned_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # Archived - the pin's sibling: user-space triage that hides a settled task from default
    # lists (every list filters on it; `archived=true` is the drawer). Terminal-only at the
    # operation, so live work can never vanish mid-flight. NULL = active.
    archived_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # The fork's hold (user law v4) - pin/archive's sibling, user-space and
    # never a machine status: a held task is born `created`, the sweeper skips it, nothing
    # runs; the first steering directive clears the hold and launches it.
    held_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TaskStatus.CREATED)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    clarify: Mapped[str] = mapped_column(String(8), nullable=False, default="auto")

    # ONE nullable FK. A task is created anywhere and moves in/out/between projects freely.
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    creator_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="RESTRICT"), nullable=False
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )

    # Routine provenance: set when a routine minted this task. The creator stays the
    # routine's HUMAN owner (the invariant above holds - attribution never forks); these columns
    # carry the machine origin. `origin_routine_id` is what makes per-routine spend a ledger
    # JOIN (the per-bot precedent), and `origin_depth` is the loop budget the event lane's
    # chain-depth guard reads (the Exchange hop cap).
    origin_routine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routines.id", ondelete="SET NULL"), default=None
    )
    # A chat-born task's provenance - the origin_routine_id twin, and the REPLACEMENT
    # of the old `origin_channel_id` (a channel is now a transport for a conversation, so a
    # channel-born task reaches its channel through `conversations.channel_id` - one origin
    # column per machine, never two). `origin_message_seq` names the message that started it.
    origin_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), default=None
    )
    origin_message_seq: Mapped[int | None] = mapped_column(default=None)
    origin_depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")

    # **The one intake round's answers**. At most one round per task, so one column -
    # not a history. What the human said becomes `/task/answers.json` in every step's sandbox and
    # part of the planner's brief, which is the whole reason to ask at all.
    answers: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    criteria: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    budget: Mapped[dict[str, Any] | None] = mapped_column(default=None)

    # The duplicate-task guardrail: a client-minted token per SEND GESTURE.
    # create_task returns the existing row for a replayed token (same creator), and the
    # partial unique index makes the true race a constraint error - never a twin task.
    client_token: Mapped[str | None] = mapped_column(String(64), default=None)

    # Fleet seat selection: the catalog refs the creator chose for this task's fleet,
    # or NULL for "every eligible enabled model". Validated against the catalog at submit;
    # planning intersects with eligibility again, so a ref disabled since submit costs the
    # seat, never the task.
    model_refs: Mapped[list[str] | None] = mapped_column(default=None)

    # The attachments, as `[TaskInput, …]` - filename + sha256 + size. **The bytes are not here.**
    # They are in the artifact store under their own hash, exactly like an artifact, because that
    # is what makes them checkable: the verifier re-derives from the same bytes the writer read,
    # and the key *is* the hash, so it cannot be a different file wearing the same name.
    inputs: Mapped[list[dict[str, Any]]] = mapped_column(nullable=False, default=list, server_default="[]")

    # The skills this task requested, resolved to concrete versions at submit time. The
    # planner copies these onto every executable step's `skill_pins`, and the executor loads them into
    # the frame. Resolving to a *version* here - not a name - is the reproducibility guarantee: a task
    # that pinned report@1 keeps loading report@1 after report@2 publishes.
    skill_pins: Mapped[list[dict[str, Any]]] = mapped_column(
        nullable=False, default=list, server_default="[]"
    )

    # The connectors this task may reach, resolved to row ids at submit time - refs
    # (ids or slugs) are what the wire speaks; ids are what the step loads. Task-level on
    # purpose: every executable step of this task gets the same toolsets (per-step scoping is
    # the recorded deferral), and a connector that dies mid-thread costs the capability at the
    # next mount, never the task.
    connector_ids: Mapped[list[str]] = mapped_column(nullable=False, default=list, server_default="[]")
    # The subset of `connector_ids` mounted by INTENT ATTACH, not an explicit user
    # act: at mount their non-read-only tools gate on approval even under the ALLOW dial - the
    # "connecting was the consent" rule holds for a clicked Connect, not an auto-mount.
    connectors_auto: Mapped[list[str]] = mapped_column(nullable=False, default=list, server_default="[]")

    # The bots this task explicitly attached, resolved to versions at submit time.
    # EMPTY IS NOT "NO BOTS": the roster is auto-included from the task's Project and its creator's
    # own published bots, because a roster you must remember to attach is not a roster. This list
    # is the override - name bots here and One is shown exactly those, which is how you pin a
    # draft (`reviewer@draft`, author-only) or narrow a crowded space.
    bot_pins: Mapped[list[dict[str, Any]]] = mapped_column(nullable=False, default=list, server_default="[]")

    # The DM lane: non-null names the ONE bot that answers every turn of this task,
    # deterministically - planning takes the code-made one-step path and the planner is
    # structurally unreachable. NULL = the normal lanes, untouched.
    direct_bot: Mapped[str | None] = mapped_column(String(64), default=None)

    # The task's artifact kind (the path law). NULL = not yet resolved;
    # set = a CONSTRAINT: the final publishing step must be an agent that publishes this kind,
    # and the fallback floor becomes that agent - a dropdown's promise, kept deterministically.
    deliverable: Mapped[str | None] = mapped_column(String(24), default=None)
    # WHO chose the kind: "user" (the dial) | "auto" (the goal's own language,
    # detected by the kind law) | "plan" (stamped from the validated plan's final publishing
    # step). Only "user" earns the planner-skipping fast path - a wrong auto guess must never
    # become an unappealable one-step plan. NULL with a set deliverable = legacy row = "user".
    deliverable_source: Mapped[str | None] = mapped_column(String(8), default=None)
    # The document's FILE FORMAT: "md" | "docx" | "pdf" | "html".
    # Documents only (a deck IS .pptx); NULL = unresolved - words decide at planning, and
    # markdown is delivery's default. Resolved per turn like every dial: dial > words > stamp.
    deliverable_format: Mapped[str | None] = mapped_column(String(8), default=None)

    # The requirement dial: economy | balanced | max. NULL = balanced = the pre-dial
    # chain order, byte for byte. Re-ranks only the UNCHAINED tail of executor chains by a static
    # price comparator; explicitly-chained refs are operator intent and never move; the judge
    # lane is exempt (verification integrity is not a cost dial).
    quality: Mapped[str | None] = mapped_column(String(16), default=None)

    # The driver this task's Machine runs on: `device` in local mode,
    # set once at create; NULL = the worker's `frame_driver`, as every task before it. It wins over
    # the worker's, so a task never moves between the device and a cloud Machine.
    machine_driver: Mapped[FrameDriverName | None] = mapped_column(
        StrEnumColumn(FrameDriverName, 20), default=None
    )

    # The long-form lane's door. Stored RAW: "auto" is a real state until intake
    # detection resolves it to standard|long (an explicit "standard" must stay distinguishable -
    # it NEVER enters the lane). `long` plans deterministically (architect → sections ∥ →
    # assembly) and has no one-step fallback. `target_pages` sizes the outline and the estimate.
    scale: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    target_pages: Mapped[int | None] = mapped_column(default=None)

    # **The Context Pack, frozen per TURN at that turn's planning** (mirrors `answers`): what this
    # task was told beyond its ask - project instructions, memories, the thread pointer - identical
    # across retries, replans and resumes *within a turn*, and exactly what that turn's
    # `context.assembled` event recorded. A follow-up's planning re-assembles and overwrites it;
    # recomputing at execution time would let the mounted file drift from the record, which the
    # observability story cannot allow. NULL until planned; NULL when every lane was empty.
    context_pack: Mapped[dict[str, Any] | None] = mapped_column(default=None)

    # The high-water mark of this task's event sequence. Bumped inside the same transaction as the
    # insert, so `(task_id, seq)` is dense and gap-free - which is what makes `?since=` a replay
    # cursor rather than an approximation.
    event_seq: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")

    # Which conversation turn this task is on, 1-based. A follow-up bumps it and
    # re-enters planning on the SAME row - the thread is the task. The counter exists for the
    # per-turn workflow id (`task-{id}` for turn 1, `task-{id}-t{n}` after): a completed DBOS
    # workflow id never re-executes, so each turn must run under its own.
    turn: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    # The author of the CURRENT turn: the creator at birth, the follow-up's sender after.
    # The ledger copies it into usage_events.actor_id in SQL - spend is labeled, never charged, per person.
    turn_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    # The plan epoch: moves at every turn's start and every accepted steer, only under the
    # task row lock, never backwards. `turn_epoch` is the epoch the current turn opened at;
    # `epoch_actor_id` is who opened the current epoch (the turn's author, then each steerer) - the
    # spend label for usage that names no step.
    plan_epoch: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    turn_epoch: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    epoch_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ── the machine ───────────────────────────────────────────────────────────────────────────

    @property
    def state(self) -> TaskStatus:
        return TaskStatus(self.status)

    @property
    def user_deliverable(self) -> str | None:
        """The deliverable kind a person dialed (a legacy row with no source reads as theirs); None otherwise."""
        return self.deliverable if (self.deliverable_source or "user") == "user" else None

    def can(self, to: TaskStatus) -> bool:
        return to in ALLOWED[self.state]

    def transition(self, to: TaskStatus) -> None:
        """The only way a task changes status. Raises rather than drifting."""
        if not self.can(to):
            raise InvalidTransition(
                f"task {self.id} cannot go {self.status} → {to}",
                task_id=str(self.id),
                from_status=self.status,
                to_status=to.value,
            )
        self.status = to.value

    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def stays_paused(self, to: TaskStatus) -> bool:
        """A paused task stays paused until a person resumes it: the machine's own moves
        between running and waiting - a step parking on a gate, a gate's answer - leave it
        `paused`, and the resume puts it where the parks left it (`waiting` while a gate is still
        open). A stop still lands: cancelled and failed are not moves this rule keeps."""
        return self.state is TaskStatus.PAUSED and to in (TaskStatus.RUNNING, TaskStatus.WAITING)

    # ── the only fetch paths ──────────────────────────────────────────────────────────────────

    @classmethod
    def _visible_to(cls, actor: Actor, workspace_id: uuid.UUID) -> ColumnElement[bool]:
        """Visibility as a WHERE predicate, inside ONE workspace: yours, or filed
        in a project you belong to - **and the project's owner sees the whole room.** The workspace conjunct
        lives here so no list or count can forget it: a person in two workspaces sees only the bound
        workspace's tasks, whoever created them.

        The owner is a column on the project, not a `project_members` row ("the owner outranks any
        membership row"), so it takes its own EXISTS - without it an owner could not see, list, or
        count a task a member filed in their own project, which is the opposite of what owning the
        room means. EXISTS, deliberately not a join: joining `project_members` fans each task out
        once per member and lets `limit` count copies instead of tasks.
        """
        return and_(
            cls.workspace_id == workspace_id,
            or_(
                cls.creator_actor_id == actor.id,
                select(ProjectMember.actor_id)
                .where(ProjectMember.project_id == cls.project_id, ProjectMember.actor_id == actor.id)
                .exists(),
                select(Project.id)
                .where(Project.id == cls.project_id, Project.owner_actor_id == actor.id)
                .exists(),
            ),
        )

    @classmethod
    async def unsettled_of(
        cls, session: AsyncSession, creator_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(task id, workspace id) of each unsettled task the creator started, in every workspace."""
        live = cls.creator_actor_id == creator_id, cls.status.not_in([s.value for s in TERMINAL])
        return list((await session.execute(select(cls.id, cls.workspace_id).where(*live))).tuples())

    @classmethod
    async def by_client_token(cls, session: AsyncSession, actor_id: uuid.UUID, token: str) -> Task | None:
        """The idempotency lookup (guardrail): the row a replayed send gesture
        already minted - same creator, same token, same (bound) workspace. Creator-scoped by
        construction, so no cross-actor probe is possible; workspace-scoped, so a replay never answers
        with a task of another workspace (the unique key is per workspace to match)."""
        return await session.scalar(
            select(cls).where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.creator_actor_id == actor_id,
                cls.client_token == token,
            )
        )

    @classmethod
    async def drivers_in_project(
        cls, session: AsyncSession, project_id: uuid.UUID
    ) -> list[FrameDriverName | None]:
        """Each distinct `machine_driver` the project's tasks record, in the bound workspace."""
        query = (
            select(cls.machine_driver)
            .where(cls.workspace_id == tenant.workspace_of(session), cls.project_id == project_id)
            .distinct()
        )
        return list(await session.scalars(query))

    @classmethod
    async def live_for_routine(cls, session: AsyncSession, routine_id: uuid.UUID) -> Task | None:
        """The overlap gate's read: this routine's newest un-settled minted task -
        HELD tasks count too (nothing runs, but approvals must not pile). Rides
        `ix_tasks_origin_routine_id`."""
        return await session.scalar(
            select(cls)
            .where(
                cls.origin_routine_id == routine_id,
                cls.status.not_in([s.value for s in TERMINAL]),
            )
            .order_by(cls.created_at.desc())
            .limit(1)
        )

    @classmethod
    async def hold(cls, session: AsyncSession, task_id: uuid.UUID) -> None:
        """Take the task's row lock, to COMMIT - what a writer does BEFORE it reads or writes the
        task's gates or memories, the one order every task writer uses (task first), so two of
        them never meet the other way round. `FOR NO KEY UPDATE`: the
        mode `next_seq`'s own UPDATE takes - it changes no key column - so it still queues
        against every seq claim, while a child row's insert (the FK's KEY SHARE: events, usage,
        gates, plan steps) is not blocked behind it. The ORM object is left as it is (`held`
        reads it again)."""
        await session.execute(select(cls.id).where(cls.id == task_id).with_for_update(key_share=True))

    @classmethod
    async def held(cls, session: AsyncSession, task_id: uuid.UUID) -> Task:
        """`hold`, and the row as the lock sees it: every status decision reads here, never a copy
        this session loaded before the lock (`expire_on_commit=False` keeps one indefinitely), or
        a pause lands on a delivered task and a delivery over a cancel. The session's own
        instance is refreshed in place (`populate_existing`), so a caller holding it reads on
        through it - call it before this unit of work writes the task: unflushed changes to the
        row are replaced too."""
        task = await session.scalar(
            select(cls)
            .where(cls.id == task_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise NotFound("no such task")
        return task

    @classmethod
    async def lock(cls, session: AsyncSession, task_id: uuid.UUID) -> Task | None:
        """The row, FOR UPDATE - taken by a walk that will write the task AND its conversation,
        in the order every gate opener takes them (tasks, then conversations), so the two
        walks never deadlock (found in review)."""
        return await session.scalar(select(cls).where(cls.id == task_id).with_for_update())

    @classmethod
    async def get_for(cls, session: AsyncSession, task_id: uuid.UUID, actor: Actor) -> Task:
        """The ONLY way to fetch a task. Scope-aware, and it raises NotFound - never Forbidden.

        Visibility is exact: a spaceless task is visible only to its creator; a task in
        a project is visible to every member of that project. A 403 on a task you cannot see would
        confirm the task exists, so existence is not leaked - it simply is not there.
        """
        task = await session.scalar(
            select(cls).where(cls.id == task_id, cls._visible_to(actor, tenant.workspace_of(session)))
        )
        if task is None:
            raise NotFound("no such task")
        return task

    @classmethod
    async def any_input_with_digest(cls, session: AsyncSession, sha256: str) -> bool:
        """Does any task mount this content-addressed blob as an INPUT? Inputs are a JSONB
        list of `{filename, sha256, size}`; the `@>` containment test asks Postgres whether any
        row's array holds an element with this digest. The second half of the blob-GC guard: an
        attachment can be one task's input and another's artifact, and a delete of one must never
        strand the other."""
        return (
            await session.scalar(select(cls.id).where(cls.inputs.contains([{"sha256": sha256}])).limit(1))
            is not None
        )

    @classmethod
    def search_filter(cls, q: str) -> ColumnElement[bool]:
        """`q` against title OR goal, case-insensitive, wildcards as literals - one definition,
        so the list, its counts, and the admin console can never disagree about what matches.
        (Public since the admin surface: `operations/admin_ops.list_tasks_admin` is a legitimate
        third caller, and a leading underscore it must reach around is a lie about its scope.)"""
        needle = f"%{escape_like(q)}%"
        return or_(cls.title.ilike(needle, escape="\\"), cls.goal.ilike(needle, escape="\\"))

    @classmethod
    async def list_for(
        cls,
        session: AsyncSession,
        actor: Actor,
        *,
        statuses: list[str] | None = None,
        project_id: uuid.UUID | None = None,
        mode: str | None = None,
        pinned: bool | None = None,
        archived: bool = False,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Task]:
        """Keyset pagination on `(created_at, id)` - never OFFSET.

        An OFFSET of 10,000 makes Postgres read 10,000 rows and throw them away; a keyset reads
        the page. The tuple `(created_at, id)` is the cursor because `created_at` alone is not
        unique and a duplicate timestamp would silently drop or repeat a row at a page boundary.

        `statuses` is a set, not a scalar, because the product groups the machine's nine states
        into a few human ones (in progress · delivered · failed & cancelled) and each group is
        its own independently-paginated list - one IN-query per group, never a client-side sieve.
        """
        query = (
            select(cls)
            .where(cls._visible_to(actor, tenant.workspace_of(session)))
            # The archive drawer is a VIEW, not a filter you can forget: default lists are
            # the active world, `archived=True` is only the archived one - never both.
            .where(cls.archived_at.isnot(None) if archived else cls.archived_at.is_(None))
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if statuses:
            query = query.where(cls.status.in_(statuses))
        if project_id is not None:
            query = query.where(cls.project_id == project_id)
        if mode is not None:
            query = query.where(cls.mode == mode)
        if pinned is not None:
            query = query.where(cls.pinned_at.isnot(None) if pinned else cls.pinned_at.is_(None))
        if q:
            query = query.where(cls.search_filter(q))
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))

    @classmethod
    async def counts_for(
        cls,
        session: AsyncSession,
        actor: Actor,
        *,
        project_id: uuid.UUID | None = None,
        mode: str | None = None,
        archived: bool = False,
        q: str | None = None,
    ) -> dict[str, int]:
        """How many visible tasks sit in each status - one GROUP BY under the same visibility
        and search filters as `list_for` (including the archive view), so a group header can
        state the true total while the list below it holds only a page."""
        query = (
            select(cls.status, func.count())
            .where(cls._visible_to(actor, tenant.workspace_of(session)))
            .where(cls.archived_at.isnot(None) if archived else cls.archived_at.is_(None))
            .group_by(cls.status)
        )
        if project_id is not None:
            query = query.where(cls.project_id == project_id)
        if mode is not None:
            query = query.where(cls.mode == mode)
        if q:
            query = query.where(cls.search_filter(q))
        rows = await session.execute(query)
        # .tuples().all() first: a bare Result has .keys(), so dict(rows) would treat it as
        # a mapping (and Row defeats dict()'s pair overload for the type checker).
        return dict(rows.tuples().all())

    # ── the operator's queries (admin surface) ────────────────────────────────────────────────
    # The console deliberately does NOT ride `_visible_to` - it is the separate, audited door
    # (`operations/admin_ops`, law 1). The SQL lives here because every query in the product
    # lives on its fat model; the POLICY (who may call this, what gets audited) lives in
    # the operation. Local imports below break no layer: models may know models.

    @classmethod
    def _admin_last_event_at(cls) -> Any:
        from prismoid.db.models.event import TaskEvent

        return select(func.max(TaskEvent.at)).where(TaskEvent.task_id == cls.id).scalar_subquery()

    @classmethod
    def _admin_spend(cls) -> Any:
        from prismoid.db.models.usage import UsageEvent

        return (
            select(func.coalesce(func.sum(UsageEvent.cost_usd), 0))
            .where(UsageEvent.task_id == cls.id)
            .scalar_subquery()
        )

    @classmethod
    def admin_stuck_filter(
        cls, now: dt.datetime, *, stale_s: int, undispatched_s: int
    ) -> ColumnElement[bool]:
        """The stuck predicate, in SQL - ONE definition, shared by the console filter, the count,
        and (through `admin_ops.stuck_reason`) the row's diagnosis line.

        (a) running/waiting with no task_event for `stale_s` - the machine claims motion, the
            record shows none (a wedged step, a dead worker, a park nothing will wake).
        (b) created/clarifying/planning older than `undispatched_s` and not deliberately held -
            never took off, and the sweeper's belts have already had their chance.
        Paused and gate-parked-with-a-live-TTL tasks are NOT stuck: a human chose that state.
        """
        stale = now - dt.timedelta(seconds=stale_s)
        young = now - dt.timedelta(seconds=undispatched_s)
        active = [TaskStatus.RUNNING.value, TaskStatus.WAITING.value]
        larval = [TaskStatus.CREATED.value, TaskStatus.CLARIFYING.value, TaskStatus.PLANNING.value]
        return or_(
            (cls.status.in_(active)) & (func.coalesce(cls._admin_last_event_at(), cls.updated_at) < stale),
            (cls.status.in_(larval)) & cls.held_at.is_(None) & (cls.updated_at < young),
        )

    @classmethod
    async def admin_page(
        cls,
        session: AsyncSession,
        *,
        now: dt.datetime,
        stale_s: int,
        undispatched_s: int,
        statuses: list[str] | None = None,
        project_id: uuid.UUID | None = None,
        creator_id: uuid.UUID | None = None,
        stuck: bool = False,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[tuple[Task, Any, dt.datetime | None, Any]]:
        """The cross-user console page: (task, owner User | None, last_event_at, spend_usd)."""
        from prismoid.db.models.user import User

        query = (
            select(cls, User, cls._admin_last_event_at(), cls._admin_spend())
            .join(User, User.actor_id == cls.creator_actor_id, isouter=True)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if statuses:
            query = query.where(cls.status.in_(statuses))
        if project_id is not None:
            query = query.where(cls.project_id == project_id)
        if creator_id is not None:
            query = query.where(cls.creator_actor_id == creator_id)
        if stuck:
            query = query.where(cls.admin_stuck_filter(now, stale_s=stale_s, undispatched_s=undispatched_s))
        if q:
            # A pasted task id is the operator's most common search - an exact match, not a
            # substring hunt through goals that happen to contain hex.
            try:
                query = query.where(cls.id == uuid.UUID(q.strip()))
            except ValueError:
                query = query.where(cls.search_filter(q))
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return [(t, u, at, spend) for t, u, at, spend in (await session.execute(query)).tuples().all()]

    @classmethod
    async def admin_row(
        cls, session: AsyncSession, task_id: uuid.UUID
    ) -> tuple[Task, Any, dt.datetime | None, Any] | None:
        """One task through the admin door, same tuple shape as `admin_page` - or None."""
        from prismoid.db.models.user import User

        row = (
            await session.execute(
                select(cls, User, cls._admin_last_event_at(), cls._admin_spend())
                .join(User, User.actor_id == cls.creator_actor_id, isouter=True)
                .where(cls.id == task_id)
            )
        ).first()
        return None if row is None else (row[0], row[1], row[2], row[3])

    @classmethod
    async def admin_stuck_count(
        cls, session: AsyncSession, *, now: dt.datetime, stale_s: int, undispatched_s: int
    ) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.admin_stuck_filter(now, stale_s=stale_s, undispatched_s=undispatched_s))
            )
            or 0
        )

    @classmethod
    async def counts_for_creator(cls, session: AsyncSession, creator_id: uuid.UUID) -> dict[str, int]:
        """One user's tasks by status - the admin user-detail's task block."""
        rows = await session.execute(
            select(cls.status, func.count()).where(cls.creator_actor_id == creator_id).group_by(cls.status)
        )
        return dict(rows.tuples().all())

    # (`settled_count_since` lived here and is gone: "reached this status since" cannot be read
    #  off `updated_at`, which `onupdate=now()` re-dates on every pin, move, and rename. The
    #  append-only record answers it honestly - `TaskEvent.settled_counts_since`.)

    def cursor(self) -> str:
        """The keyset cursor for this row. Opaque to the client, meaningful to the index."""
        return keyset_cursor(self)

    async def next_seq(self, session: AsyncSession) -> int:
        """Claim the next event sequence number, inside the caller's transaction.

        `UPDATE … SET event_seq = event_seq + 1 RETURNING` takes a row lock, so two concurrent
        writers cannot claim the same seq - which is what makes `(task_id, seq)` dense and gap-free,
        and therefore what makes `?since=<seq>` an exact replay cursor rather than a hopeful one.
        A `max(seq)+1` subquery would race here and lose events under exactly the concurrency the
        frontier fan-out creates.

        ────────────────────────────────────────────────────────────────────────────────────────
        **That row lock is held until COMMIT, and the caller had better be about to commit.**

        Under the parallel fan-out two steps run in two transactions at once. The first one to append
        *any* durable frame takes this lock - and if its transaction then runs a language model for
        four minutes before committing, the other branch blocks on `UPDATE tasks SET event_seq …`
        for four minutes. That is not a slow fan-out, it is a **deadlock**: both writers sat in
        `running`, emitted nothing, and `pg_stat_activity` showed the second one waiting on a
        `transactionid` lock, on this statement, for as long as anybody cared to watch.

        So: a frame emitted *during* a step (a tool call, a usage record, a snapshot) is appended in
        its **own short transaction** - see `execution._record`. Which is also simply right. The
        flight recorder and the ledger are append-only facts about what happened, and binding them
        to the step's outcome means **a failed step rolls back its own record** - the very evidence
        someone needs to find out why it failed.
        """
        seq = await session.scalar(
            update(Task)
            .where(Task.id == self.id)
            .values(event_seq=Task.event_seq + 1)
            .returning(Task.event_seq)
        )
        if seq is None:
            raise NotFound(f"task {self.id} is gone; no event seq to claim")
        self.event_seq = seq
        return seq

    @staticmethod
    async def lock_sources(session: AsyncSession, task_id: uuid.UUID) -> None:
        """Serialize the task's source ids until this transaction ends: two steps reading one page get one id."""
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"sources:{task_id}"))))

    @classmethod
    async def claim_seq(cls, session: AsyncSession, task_id: uuid.UUID) -> int:
        """`next_seq` without a loaded row - for the short-transaction append path."""
        seq = await session.scalar(
            update(cls).where(cls.id == task_id).values(event_seq=cls.event_seq + 1).returning(cls.event_seq)
        )
        if seq is None:
            raise NotFound("no such task")
        return seq

    @classmethod
    async def labels_for(cls, session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        """id → display label (the minted/user title, else the goal) for a batch of tasks -
        one IN query (the `User.by_actor_ids` idiom). The inbox names its rows by the TASK;
        a notification's own title is the event, not the thing."""
        if not ids:
            return {}
        rows = (await session.execute(select(cls.id, cls.title, cls.goal).where(cls.id.in_(ids)))).all()
        return {row.id: (row.title or row.goal) for row in rows}

    @classmethod
    async def by_ids(cls, session: AsyncSession, ids: set[uuid.UUID]) -> list[Task]:
        """A batch of the bound workspace's tasks by id - one IN query. No visibility rule beyond the
        tenant: the caller already holds rows that name them (a queue of the caller's own gates)."""
        if not ids:
            return []
        workspace = tenant.workspace_of(session)
        return list(await session.scalars(select(cls).where(cls.workspace_id == workspace, cls.id.in_(ids))))

    @classmethod
    async def stale_created(
        cls, session: AsyncSession, older_than: dt.datetime, limit: int = 100
    ) -> list[Task]:
        """Tasks that were committed but never started - what the sweeper hunts.

        A task stuck in `created` is the one failure a user cannot see and cannot retry: the API
        said 202, and then nothing happened. So the machine notices instead of them.
        """
        rows = await session.scalars(
            select(cls)
            .where(
                cls.status == TaskStatus.CREATED.value,
                cls.created_at < older_than,
                cls.held_at.is_(None),  # a HELD fork is parked on purpose, not orphaned
            )
            .order_by(cls.created_at)
            .limit(limit)
        )
        return list(rows)

    @classmethod
    async def stale_waiting(
        cls, session: AsyncSession, older_than: dt.datetime, limit: int = 100
    ) -> list[Task]:
        """Tasks sitting in `waiting` with nothing moving them: the un-flip after a
        parked question can be lost when a worker dies on the park, leaving a task whose steps
        are all settled and whose gates are all closed stranded one illegal edge from delivery.
        The sweeper inspects each candidate's steps and gates before touching anything - this
        query only shortlists by status and quiet time."""
        return await cls._quiet_in(session, TaskStatus.WAITING, older_than, limit)

    @classmethod
    async def stale_planning(
        cls, session: AsyncSession, older_than: dt.datetime, limit: int = 100
    ) -> list[Task]:
        """Tasks sitting in `planning` with nothing moving them - `stale_created`'s sibling for
        continuations. `continue_task` has the same one crash window `create_task`
        has (commit → enqueue), and the same cure: sweep and re-enqueue under the deterministic
        per-turn id, where DBOS dedupe makes rescuing a LIVE planning task a no-op. `updated_at`
        refreshes on every event append (`next_seq` is an UPDATE on this row), so a task that is
        actually writing its plan never looks stale - only one whose turn truly lost its enqueue
        (or whose worker died before DBOS journalled the workflow) sits here past the horizon.
        """
        return await cls._quiet_in(session, TaskStatus.PLANNING, older_than, limit)

    @classmethod
    async def _quiet_in(
        cls, session: AsyncSession, status: TaskStatus, older_than: dt.datetime, limit: int
    ) -> list[Task]:
        """Tasks in `status` whose row has not moved since `older_than`, the quietest first."""
        query = select(cls).where(cls.status == status.value, cls.updated_at < older_than)
        return list(await session.scalars(query.order_by(cls.updated_at).limit(limit)))
