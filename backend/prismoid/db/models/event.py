# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""TaskEvent - one append-only, per-Task-sequenced state change. The flight recorder.

`ORDER BY seq` *is* the history of a task. The primary key is `(task_id, seq)`, the
sequence is assigned application-side inside the writing transaction, and nothing ever updates a
row. That combination is what makes `?since=<seq>` an exact replay cursor instead of a hopeful
one - and it is why `Last-Event-ID` can be handed straight to the query.

`append()` drives the `pg_notify` fan-out that feeds the stream.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

import orjson
from sqlalchemy import ForeignKey, Index, Integer, String, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base
from prismoid.db.models.task import Task
from prismoid.schemas.events import DurableEvent, DurableEventAdapter
from prismoid.schemas.steering import HELD

# Postgres refuses a NOTIFY payload over 8000 bytes. A `task.planned` carrying a twelve-step plan
# can exceed that - so the fan-out sends the event inline when it fits and a `{task_id, seq}`
# pointer when it does not, and the hub reads the row back for pointers. The alternative (always
# sending a pointer) would put a SELECT on the path of every event, on the hottest loop we have.
NOTIFY_CHANNEL = "prismoid_events"
NOTIFY_MAX_BYTES = 7000  # 8000, minus room for the JSON envelope around the payload

# `replay()` reads one PAGE, not one history. A long-form task emits thousands of durable frames
# (every tool call, usage record, and snapshot is one), and a single capped SELECT silently
# truncated everything past row 1000 for any client joining late - a correctness hole, not a
# tuning knob. Callers loop: `since = page[-1].seq` until a page comes back short. The constant
# is the page size and the contract between this model and its two pagers
# (`api/stream._events`, `operations/tasks.replay_events`).
REPLAY_PAGE = 1000


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        # BRIN on `at`: the table is append-only and physically time-ordered, which is the exact
        # shape BRIN is for - a fraction of the size of a btree, for a column we only ever scan
        # in ranges.
        Index("ix_task_events_at", "at", postgresql_using="brin"),
        # Rule 10's `step_id`, indexed for the SET NULL cascade a replan fires when it drops
        # `plan_steps` - unindexed that cascade full-scanned the flight recorder (audit finding).
        # Partial: only rows still bound to a step need finding by it.
        Index("ix_task_events_step_id", "step_id", postgresql_where=text("step_id IS NOT NULL")),
        # The admin stuck predicate asks `max(at) WHERE task_id = ?` once per task. The PK is
        # `(task_id, seq)` - no `at` - so that max read every event of the task, and the stuck
        # COUNT read the whole recorder. `(task_id, at)` makes it one index row per task.
        Index("ix_task_events_task_at", "task_id", "at"),
    )

    # The composite PK is the durability contract: a duplicate seq is rejected by Postgres, not
    # by a code review.
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)

    type: Mapped[str] = mapped_column(String(40), nullable=False)

    # Rule 10: every task-scoped event carries task_id + step_id (+ project_id). They are columns,
    # not payload keys, because they are what the flight recorder is filtered and joined by.
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="SET NULL"), default=None
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), default=None
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )

    # The full typed event (schemas/events.py), exactly as it goes on the wire. One shape, stored
    # and streamed - the record and the wire format are the same thing, which is the point.
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False)

    at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    # ── the append path ───────────────────────────────────────────────────────────────────────

    @classmethod
    async def append(cls, session: AsyncSession, event: DurableEvent) -> TaskEvent:
        """Insert the event and notify the hub, inside the caller's transaction.

        The `seq` on the event must already have been claimed with `Task.next_seq()` - the two
        are separate so that a caller can build a fully-typed event *before* it writes it, rather
        than mutating a half-formed row afterwards.

        The NOTIFY is deliberately inside the transaction: Postgres delivers notifications **on
        commit**, so a listener can never see an event that a rollback then erased. That is a
        guarantee we get for free by not being clever about it.
        """
        payload = event.model_dump(mode="json")
        row = cls(
            task_id=event.task_id,
            seq=event.seq,
            type=payload["type"],
            step_id=event.step_id,
            project_id=event.project_id,
            actor_id=event.actor.id if event.actor else None,
            payload=payload,
        )
        # The ledger stamp copies its task's workspace inside the same INSERT - no read-back.
        row.workspace_id = select(Task.workspace_id).where(Task.id == event.task_id).scalar_subquery()  # pyright: ignore[reportAttributeAccessIssue]
        session.add(row)
        await session.flush()

        body = orjson.dumps(payload)
        if len(body) <= NOTIFY_MAX_BYTES:
            note = body.decode()
        else:
            # Too big for NOTIFY. Send a pointer; the hub reads the row it already committed.
            note = orjson.dumps({"task_id": str(event.task_id), "seq": event.seq, "pointer": True}).decode()

        await session.execute(
            text("select pg_notify(:channel, :payload)"),
            {"channel": NOTIFY_CHANNEL, "payload": note},
        )
        return row

    @classmethod
    async def replay(
        cls, session: AsyncSession, task_id: uuid.UUID, since: int = 0, limit: int = REPLAY_PAGE
    ) -> list[DurableEvent]:
        """One PAGE of history in `ORDER BY seq` - the meaning of `?since=`.

        Returns typed events, never rows. `Last-Event-ID` is handed straight to `since`,
        which is only sound because `(task_id, seq)` is dense and gap-free. `limit` is a page
        bound, not a history bound - a full page means there may be more, and the caller keeps
        reading from `page[-1].seq` until a page comes back short (see `REPLAY_PAGE`).
        """
        rows = await session.scalars(
            select(cls).where(cls.task_id == task_id, cls.seq > since).order_by(cls.seq).limit(limit)
        )
        return [DurableEventAdapter.validate_python(r.payload) for r in rows]

    @classmethod
    async def any_step_ran(cls, session: AsyncSession, task_id: uuid.UUID) -> bool:
        """Has a step of the task ever gone `running`? `PlanStep.any_started` forgets a step a replan
        deleted, and the record forgets nothing."""
        query = select(cls.seq).where(
            cls.task_id == task_id, cls.type == "step.state", cls.payload["state"].astext == "running"
        )
        return await session.scalar(query.limit(1)) is not None

    @classmethod
    async def of_types(
        cls,
        session: AsyncSession,
        task_id: uuid.UUID,
        types: Sequence[str],
        *,
        since: int = 0,
        until: int | None = None,
    ) -> list[DurableEvent]:
        """Every event of the given types, in seq order - the thread transcript's feed.

        Unlike `replay` there is deliberately NO limit cliff: the type filter already bounds the
        result to the conversation-shaped events, and a silently truncated conversation is worse
        than a long one (the transcript's own token budget does the honest clipping). `until`
        makes the read reproducible - the executor mounts exactly the history the planner saw,
        byte for byte, no matter how many events land mid-turn.
        """
        query = select(cls).where(cls.task_id == task_id, cls.type.in_(list(types)), cls.seq > since)
        if until is not None:
            query = query.where(cls.seq <= until)
        rows = await session.scalars(query.order_by(cls.seq))
        return [DurableEventAdapter.validate_python(r.payload) for r in rows]

    @classmethod
    async def settled_counts_since(cls, session: AsyncSession, since: dt.datetime) -> tuple[int, int]:
        """(failed, delivered) SETTLEMENTS in the window - the admin alerts' failure-rate read.

        Replaces a `tasks.updated_at >= since AND status = 'failed'` count, which was wrong in a
        way that only showed up in the alert text: `updated_at` carries `onupdate=now()`, so
        pinning, moving, or renaming a task that failed last month re-dated it into "the last
        24 h" and the alert cried about ancient history (and could flip itself to `error` on the
        strength of it). An event happened when it happened; nothing re-dates the record.

        Counts EVENTS, not rows, and that is the honest number: a task delivered, continued, and
        delivered again really did settle twice. One scan, two conditional counts - the BRIN
        index on `at` bounds it to the window's blocks.
        """
        row = (
            await session.execute(
                select(
                    func.count().filter(cls.type == "task.status", cls.payload["status"].astext == "failed"),
                    func.count().filter(cls.type == "task.delivered"),
                ).where(cls.at >= since)
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    @classmethod
    async def count_tool_calls(
        cls,
        session: AsyncSession,
        *,
        task_id: uuid.UUID,
        step_id: uuid.UUID,
        prefix: str,
        verbs: Iterable[str],
        ok: bool = True,
        attempt: int | None = None,
    ) -> int:
        """How many `tool.called` events landed for this step, for a tool named `f"{prefix}{verb}"`
        (`verbs` a recorder's vocabulary, e.g. `RECORDED_VERBS`), with the given `ok`.

        The recording suite's `format:count` check (`operations/verification.py`) needs a number
        the frame cannot know on its own - what the journal, not the sidecar, actually saw for
        this attempt. `step_id` is the real column (`ix_task_events_step_id`), never the payload
        copy, and `tool`/`ok` come off `ToolCalled`'s payload keys - there is no column for either.

        `attempt`, when given, counts only that step attempt's rows (`ToolCalled.attempt`, the
        engine's stamp): a retried step journaled its earlier attempts' calls too, and each
        attempt's recording only ever saw its own."""
        tools = [f"{prefix}{v}" for v in verbs]
        query = (
            select(func.count())
            .select_from(cls)
            .where(
                cls.task_id == task_id,
                cls.step_id == step_id,
                cls.type == "tool.called",
                cls.payload["tool"].astext.in_(tools),
                cls.payload["ok"].astext == ("true" if ok else "false"),
            )
        )
        if attempt is not None:
            query = query.where(cls.payload["attempt"].astext == str(attempt))
        return int(await session.scalar(query) or 0)

    @classmethod
    async def tools_called(
        cls, session: AsyncSession, *, task_id: uuid.UUID, step_id: uuid.UUID, attempt: int
    ) -> set[str]:
        """The tools this step attempt journaled a call of (`tool.called`, ok or not), by journal
        name - what the attempt did or may have done, for the steering fences' "may it run again"
        : a call that failed may have acted before it raised (a timeout after the
        server sent the mail). A row with no attempt stamp counts too: it could be this attempt's.
        A call a steer held is no call: it was chosen and never run (`steering.HELD`)."""
        stamp = cls.payload["attempt"].astext
        reason = func.coalesce(cls.payload["denied_reason"].astext, "")
        query = (
            select(cls.payload["tool"].astext)
            .distinct()
            .where(
                cls.task_id == task_id,
                cls.step_id == step_id,
                cls.type == "tool.called",
                (stamp == str(attempt)) | stamp.is_(None),
                ~reason.startswith(HELD, autoescape=True),
            )
        )
        return {str(tool) for tool in await session.scalars(query) if tool}

    @classmethod
    async def ok_tools_besides(
        cls, session: AsyncSession, *, task_id: uuid.UUID, step_id: uuid.UUID
    ) -> set[str]:
        """The journal names of every `ok=true` `tool.called` of this task from any step but `step_id`,
        every turn included: what the task's other steps read before or beside this one."""
        query = (
            select(cls.payload["tool"].astext)
            .distinct()
            .where(
                cls.task_id == task_id,
                cls.type == "tool.called",
                cls.step_id.is_distinct_from(step_id),
                cls.payload["ok"].astext == "true",
            )
        )
        return {str(tool) for tool in await session.scalars(query) if tool}

    @classmethod
    async def ok_tool_summaries(
        cls, session: AsyncSession, *, task_id: uuid.UUID, tools: Iterable[str]
    ) -> list[tuple[str, str]]:
        """Every distinct (tool, summary) of an `ok=true` `tool.called` of this task, for a tool
        in `tools` - from every step and every turn, a step a replan dropped included (`step_id`
        is not read). What the receipt's `links:resolve` reads its read pages from
        (`operations/verification.py`): a writer may cite what an earlier step fetched."""
        query = (
            select(cls.payload["tool"].astext, cls.payload["summary"].astext)
            .distinct()
            .where(
                cls.task_id == task_id,
                cls.type == "tool.called",
                cls.payload["tool"].astext.in_(list(tools)),
                cls.payload["ok"].astext == "true",
            )
        )
        return [(str(tool), str(summary or "")) for tool, summary in (await session.execute(query)).all()]

    @classmethod
    async def ok_tool_refs(
        cls, session: AsyncSession, *, task_id: uuid.UUID, tool: str
    ) -> list[tuple[str, str]]:
        """Every distinct (summary, ref) of an `ok=true` `tool.called` of `tool` in this task that kept
        something (`ToolCalled.ref`), from every step and every turn: the pages `web.fetch` stored."""
        query = (
            select(cls.payload["summary"].astext, cls.payload["ref"].astext)
            .distinct()
            .where(
                cls.task_id == task_id,
                cls.type == "tool.called",
                cls.payload["tool"].astext == tool,
                cls.payload["ok"].astext == "true",
                cls.payload["ref"].astext.is_not(None),
            )
        )
        return [(str(summary or ""), str(ref)) for summary, ref in (await session.execute(query)).all()]

    @classmethod
    async def last_tool_attempt(
        cls, session: AsyncSession, *, task_id: uuid.UUID, step_id: uuid.UUID
    ) -> int | None:
        """The highest `ToolCalled.attempt` this step journaled - the fallback for a caller that
        cannot tell which attempt it is asking about (None when no row carries one)."""
        query = select(func.max(cls.payload["attempt"].astext.cast(Integer))).where(
            cls.task_id == task_id,
            cls.step_id == step_id,
            cls.type == "tool.called",
            cls.payload.has_key("attempt"),
        )
        value = await session.scalar(query)
        return int(value) if value is not None else None

    @classmethod
    async def one(cls, session: AsyncSession, task_id: uuid.UUID, seq: int) -> DurableEvent | None:
        """Read a single event back - what the hub does when NOTIFY carried only a pointer."""
        row = await session.scalar(select(cls).where(cls.task_id == task_id, cls.seq == seq))
        return None if row is None else DurableEventAdapter.validate_python(row.payload)

    @classmethod
    async def has_type(cls, session: AsyncSession, task_id: uuid.UUID, type_: str) -> bool:
        """Does the record already say this happened? The idempotency guard for post-delivery
        effects: a replayed `remember_task` asks this before it runs memory_extract again."""
        row = await session.scalar(select(cls.seq).where(cls.task_id == task_id, cls.type == type_).limit(1))
        return row is not None

    @classmethod
    async def window_for_matcher(
        cls, session: AsyncSession, types: list[str], since: dt.datetime, until: dt.datetime
    ) -> list[tuple[TaskEvent, uuid.UUID, uuid.UUID | None, uuid.UUID | None, int]]:
        """The routine event matcher's ONE scan: events of the menu's wire types in
        a lagged window, each with its task's scoping + provenance facts joined in -
        (event, creator_actor_id, project_id, origin_routine_id, origin_depth). Rides the BRIN on
        `at`; the run ledger's unique occurrence makes overlapping windows free."""
        from prismoid.db.models.task import Task

        q = (
            select(cls, Task.creator_actor_id, Task.project_id, Task.origin_routine_id, Task.origin_depth)
            .join(Task, Task.id == cls.task_id)
            .where(cls.type.in_(types), cls.at > since, cls.at <= until)
            .order_by(cls.at)
        )
        return [(r[0], r[1], r[2], r[3], int(r[4])) for r in (await session.execute(q)).all()]

    @classmethod
    async def latest_of_type(
        cls,
        session: AsyncSession,
        task_id: uuid.UUID,
        type_: str,
        *,
        before: int | None = None,
        step_id: uuid.UUID | None = None,
    ) -> DurableEvent | None:
        """The newest event of one type - e.g. the parent's `task.delivered` for a follow-up's
        thread context. Typed, never a row; the `type` column makes this an index-friendly
        filter instead of a payload scan. `before` bounds the search to seqs below a boundary
        (the previous turn's last plan sits before this turn's `task.continued`).
        `step_id` narrows the search to one step's events: a guarded step's latest `step.prompt`
        names the row it is held to ."""
        query = select(cls).where(cls.task_id == task_id, cls.type == type_)
        if before is not None:
            query = query.where(cls.seq < before)
        if step_id is not None:
            query = query.where(cls.step_id == step_id)
        row = await session.scalar(query.order_by(cls.seq.desc()).limit(1))
        return None if row is None else DurableEventAdapter.validate_python(row.payload)

    @classmethod
    async def endings(cls, session: AsyncSession, tasks: Iterable[Task]) -> dict[uuid.UUID, str]:
        """Each settled task's typed ending (`done` | `partial` | `could_not`), read off its newest settle
        frame (`task.delivered`, or `task.status{failed}`): one query for a page of rows. A task whose
        settle carries no ending is absent."""
        ids = [t.id for t in tasks if t.status in ("delivered", "failed")]
        if not ids:
            return {}
        settle = (cls.type == "task.delivered") | (
            (cls.type == "task.status") & (cls.payload["status"].astext == "failed")
        )
        query = (
            select(cls.task_id, cls.payload["ending"].astext)
            .where(cls.task_id.in_(ids), cls.type.in_(["task.delivered", "task.status"]), settle)
            .order_by(cls.task_id, cls.seq.desc())
            .distinct(cls.task_id)
        )
        return {task_id: ending for task_id, ending in (await session.execute(query)).all() if ending}

    @classmethod
    async def teammate_input(
        cls,
        session: AsyncSession,
        task_id: uuid.UUID,
        creator_id: uuid.UUID,
        *,
        until: int | None = None,
        with_created: bool = False,
    ) -> str | None:
        """The first event type by which someone other than `creator_id` put a word in the task, or None.
        A fork's ancestor reads to `until` and counts its opening (`with_created`); actorless frames don't."""
        types = ("task.continued", "plan.edited", "gate.resolved")
        query = select(cls.type).where(
            cls.task_id == task_id,
            cls.type.in_((*types, "task.created") if with_created else types),
            cls.actor_id.is_not(None),
            cls.actor_id != creator_id,
        )
        if until is not None:
            query = query.where(cls.seq <= until)
        return await session.scalar(query.order_by(cls.seq).limit(1))
