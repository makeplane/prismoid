# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Context - assemble what a task is told beyond its ask, and record it like spend.

The memory system's **read path** (the write paths are `operations/memory.py` for people and
memory_extract for automation). Two calls, at planning:

    pack = await context.assemble(session, task)   # reads only - takes no locks
    ...the planner call...
    await context.record(session, task, pack)      # the writes, just before commit

`assemble` builds the five lanes under per-lane token budgets and writes **nothing** - planning
holds its transaction open across the planner's provider call, and a write here would hold row
locks (the task row via `next_seq`, the memory rows via `mark_used`) for that whole call,
blocking cancel/steer and serializing a user's concurrent tasks. `record` freezes the pack onto
the task row (mirroring `answers` - retries, replans and resumes all see the same context),
bumps the ledger's decay signal, writes the `memory_uses` join rows, and appends
`context.assembled` - in the caller's transaction, moments before its commit, so the mounted
file, the planner's brief and the durable record agree at the moment they are written.

**They can drift afterwards, in one direction only.** Moving a task into a Project
runs `fence_for_project`, which narrows the stored pack (private lanes shed) without rewriting the
record: `context.assembled` and `memory_uses` keep saying what was assembled, and the mounted
file says what a Project task may still read. The stored pack is then a subset of the event's,
never a superset. `record` fences the same way when the task it locks has meanwhile moved.

**The fence has one home: `Fence`, below.** Every door that reads or writes private context asks
it - this module's lanes and its pack walk, a conversation's user lane (`chat`), the thread's
turn-0 conversation (`thread`), memory extraction (`memory`), and a chat-born task's report home.

**Prompt-injection posture.** The shared lanes (project memory, skill notes) are human-authored,
attributed and editor-gated; the Project's charter renders as instruction and every remembered note
inside a data fence, never as a source (`ai/context/blocks.py`) - a poisoned note can neither pose
as an order nor launder itself into a citable provenance chain. Recall asks with the current turn's
own words (`ThreadTranscript.current_ask`), so a follow-up recalls for what it asks. Lane walls are `Memory.target_filter`;
a user's private rows reach only PERSONAL tasks that user created (a Project task never reads one).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.tools.memory import SearchMemory
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import Memory, MemoryOwner, MemoryScope, MemoryUse
from prismoid.db.models.project import Project
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.schemas.context import ContextItem, ContextLane, ContextPack, LaneName, lane_views
from prismoid.schemas.events import ContextAssembled
from prismoid.schemas.ids import MemoryId, ProjectId, TaskId
from prismoid.schemas.thread import ThreadTranscript
from prismoid.schemas.tokens import TOKENS

# Per-lane token budgets for the MEMORY lanes (the pack's hard ceiling for what it carries
# itself). The order below IS the priority order: instructions are never crowded out by
# memories. The thread lane is different now: the conversation is the transcript
# (`operations/thread.py`, its own budgets), and the pack's lane is a POINTER carrying the
# transcript's real token spend - so `context.assembled` stays the honest ledger without the
# pack duplicating a conversation that is re-derivable from the record.
BUDGETS: dict[LaneName, int] = {
    "instructions": 1000,
    "user": 500,
    "project": 400,
    "bot": 300,  # the DM keeper's standing state - direct tasks only
    "skills": 200,
}

FTS_LIMIT = 24  # relevance candidates per memory lane
RECENCY_LIMIT = 12  # recency candidates merged behind them
SKILLS_LIMIT = 12
SEARCH_LIMIT = 8  # notes one `memory_search` returns

# ── the fence: one rule, asked by every door ──────────────────────────────────────────────────

# The home of what is one person's own - their memory, their personal bot's ledger, their personal
# conversations - as `Fence.admits` reads it: no Project.
PERSONAL: Final = None


@dataclass(frozen=True)
class Fence:
    """The memory fence - the ONE rule every door that reads or writes private context
    asks. Work lives where its task (or conversation) lives: `project_id`, None for someone's own.
    What it reads or writes has a home too: a person's memory, a personal bot's ledger and a
    personal conversation are `PERSONAL`; a Project's notes, house rules, bot ledgers and
    conversations are that Project's. Personal work reaches everything its creator owns; work in a
    Project reaches only what is homed in THAT Project - never a person's own, never another Project's.
    A write is narrower than a read (`writes_to`): work writes only into its own home."""

    project_id: uuid.UUID | None

    @classmethod
    def of(cls, task: Task) -> Fence:
        return cls(task.project_id)

    def admits(self, home: uuid.UUID | None) -> bool:
        """Content homed at `home` (a Project's id, or `PERSONAL`) may reach this work."""
        return self.project_id is None or home == self.project_id

    def writes_to(self, home: uuid.UUID | None) -> bool:
        """This work may write into a place homed at `home` - a memory lane, a conversation, the
        channel behind one: only its own home. A Project task mints nobody's private memory and
        writes into no one's private conversation (its turns are its teammates' as much as its
        creator's, so they stay on the task's own thread); a personal task - one moved out of a
        Project included - never writes into a Project's."""
        return home == self.project_id

    async def narrow(self, session: AsyncSession, task: Task, pack: ContextPack) -> ContextPack | None:
        """`pack` as this work may read it: the same object when nothing was shed, the narrowed
        pack (totals recomputed) otherwise, None when nothing is left. Personal work reads its
        creator's pack whole. In a Project a lane must PROVE its home is this Project: `user` never
        can; `bot` only as this Project's own bot's ledger (the carrier `assemble` picks, checked on
        the rows too, so a same-named Project bot cannot launder a personal bot's lane); `project` by
        its rows; `instructions` as this Project's house rules verbatim. `skills` and `thread` are
        nobody's private. The queries run only for the lanes that need them, so the normal path
        stays cheap; unsure means dropped, the safe direction."""
        if self.project_id is None:
            return pack
        project_id = self.project_id
        kept: list[ContextLane] = []
        for lane in pack.lanes:
            if lane.lane == "user":
                continue
            if lane.lane == "bot" and not (
                await _project_bot(session, task, project_id)
                and await _project_owned(session, lane, project_id)
            ):
                continue
            if lane.lane == "project" and not await _project_owned(session, lane, project_id):
                continue
            if lane.lane == "instructions" and [i.text for i in lane.items] != [
                await _house_rules(session, project_id)
            ]:
                continue
            kept.append(lane)
        if len(kept) == len(pack.lanes):
            return pack
        if not kept:
            return None
        return ContextPack(lanes=kept, total_tokens=sum(lane.tokens for lane in kept))


# ── the pure half: deterministic, unit-tested without a database ──────────────────────────────


def fit_items(items: list[ContextItem], budget: int) -> tuple[list[ContextItem], int, bool]:
    """Greedy fill in the given order; the first item that would blow the budget stops the lane.
    Truncation is reported, never silent - the lane says it was clipped and the event records it."""
    kept: list[ContextItem] = []
    total = 0
    for item in items:
        tokens = TOKENS.count(item.text)
        if total + tokens > budget:
            return kept, total, True
        kept.append(item)
        total += tokens
    return kept, total, False


def thread_note(turns: int, elided: int) -> str:
    """The thread lane's one line on the receipt: what the conversation held. It names no path - the
    receipt is read by people, and where the transcript is mounted is the engine's business."""
    note = f"The conversation so far - {turns} {'turn' if turns == 1 else 'turns'}"
    return f"{note}, {elided} elided" if elided else note


def clip_head(text: str, budget: int) -> tuple[str, int, bool]:
    """Keep the opening - instructions lead with what matters most."""
    chars = budget * 4
    if len(text) <= chars:
        return text, TOKENS.count(text), False
    return text[:chars], budget, True


def clip_tail(text: str, budget: int) -> tuple[str, int, bool]:
    """Keep the ending - the end of a delivered summary is its conclusion."""
    chars = budget * 4
    if len(text) <= chars:
        return text, TOKENS.count(text), False
    return f"…{text[-chars:]}", budget, True


def merge_unique(*groups: list[Memory]) -> list[Memory]:
    """Relevance first, recency behind it, each row once - RRF's spirit without its ceremony."""
    seen: set[object] = set()
    out: list[Memory] = []
    for group in groups:
        for m in group:
            if m.id not in seen:
                seen.add(m.id)
                out.append(m)
    return out


def memory_lane(
    name: LaneName, rows: list[Memory], budget: int, *, label: str | None = None
) -> ContextLane | None:
    items = [ContextItem(text=m.text, memory_id=m.id, kind=m.kind) for m in rows]
    kept, tokens, truncated = fit_items(items, budget)
    if not kept:
        return None
    return ContextLane(lane=name, items=kept, tokens=tokens, truncated=truncated, label=label, budget=budget)


# ── assembly: the five lanes, then the record ─────────────────────────────────────────────────


async def assemble(
    session: AsyncSession, task: Task, *, thread: ThreadTranscript | None = None
) -> ContextPack | None:
    """Build the task's Context Pack - **reads only, no locks** (see the module docstring for
    why; `record` is the write half). Returns None when every lane is empty: silence is not
    padded into ceremony. `thread` is the turn's transcript (already rendered by the caller);
    the pack's thread lane points at it and records its real token spend."""
    fence = Fence.of(task)
    ask = (thread.current_ask if thread is not None else None) or task.goal
    lanes: list[ContextLane] = []

    if task.project_id is not None:
        project = await session.get(Project, task.project_id)
        if project is not None and project.instructions:
            text, tokens, truncated = clip_head(project.instructions, BUDGETS["instructions"])
            lanes.append(
                ContextLane(
                    lane="instructions",
                    items=[ContextItem(text=text)],
                    tokens=tokens,
                    truncated=truncated,
                    budget=BUDGETS["instructions"],
                )
            )

    if fence.admits(PERSONAL):  # a Project task never reads anyone's private memory
        lane = memory_lane(
            "user",
            merge_unique(
                await Memory.search(
                    session,
                    MemoryScope.USER,
                    user_id=task.creator_actor_id,
                    query_text=ask,
                    limit=FTS_LIMIT,
                ),
                await Memory.for_scope(
                    session, MemoryScope.USER, user_id=task.creator_actor_id, limit=RECENCY_LIMIT
                ),
            ),
            BUDGETS["user"],
        )
        if lane:
            lanes.append(lane)

    if task.project_id is not None:
        lane = memory_lane(
            "project",
            merge_unique(
                await Memory.search(
                    session,
                    MemoryScope.PROJECT,
                    project_id=task.project_id,
                    query_text=ask,
                    limit=FTS_LIMIT,
                ),
                await Memory.for_scope(
                    session, MemoryScope.PROJECT, project_id=task.project_id, limit=RECENCY_LIMIT
                ),
            ),
            BUDGETS["project"],
        )
        if lane:
            lanes.append(lane)

    # The DM keeper's standing state - direct tasks only: at assemble time a
    # routed task's seats are unknown, and the DM lane is where the bot IS the conversation.
    # The carrier follows the BOT's scope: a project bot's memories ride the project, a personal
    # bot's ride its owner - and only on a personal task. A Project task never reads a person's
    # private lane, so a personal bot pinned there adds none.
    if task.direct_bot:
        from prismoid.db.models.bot import BotRecord

        carrier: MemoryOwner | None = None
        if task.project_id is not None and await BotRecord.all_versions(
            session, task.direct_bot, project_id=task.project_id, owner_actor_id=task.creator_actor_id
        ):
            carrier = MemoryOwner(project_id=task.project_id)
        elif fence.admits(PERSONAL) and await BotRecord.all_versions(
            session, task.direct_bot, project_id=None, owner_actor_id=task.creator_actor_id
        ):
            carrier = MemoryOwner(user_id=task.creator_actor_id)  # a personal bot's memory
        if carrier is not None:
            lane = memory_lane(
                "bot",
                merge_unique(
                    await Memory.search(
                        session,
                        MemoryScope.BOT,
                        bot_name=task.direct_bot,
                        query_text=ask,
                        limit=FTS_LIMIT,
                        **carrier,
                    ),
                    await Memory.for_scope(
                        session,
                        MemoryScope.BOT,
                        bot_name=task.direct_bot,
                        limit=RECENCY_LIMIT,
                        **carrier,
                    ),
                ),
                BUDGETS["bot"],
                label=f"What {task.direct_bot} remembers",
            )
            if lane:
                lanes.append(lane)

    # The conversation itself lives in the transcript (mounted as /task/thread.md, inlined in
    # the planner's brief); the lane is its receipt - the REAL token count, on the ledger.
    if thread is not None and not thread.is_empty():
        lanes.append(
            ContextLane(
                lane="thread",
                items=[ContextItem(text=thread_note(len(thread.turns), thread.elided_turns))],
                tokens=thread.total_tokens,
                truncated=thread.truncated,
                budget=thread.budget_tokens,
            )
        )

    pins = list(dict.fromkeys(str(p.get("name")) for p in task.skill_pins if p.get("name")))
    if pins:
        lane = memory_lane(
            "skills",
            await Memory.for_scope(session, MemoryScope.SKILL, skill_names=pins, limit=SKILLS_LIMIT),
            BUDGETS["skills"],
        )
        if lane:
            lanes.append(lane)

    if not lanes:
        return None
    return ContextPack(lanes=lanes, total_tokens=sum(lane.tokens for lane in lanes))


async def record(session: AsyncSession, task: Task, pack: ContextPack | None) -> None:
    """The record, one transaction: the pack on the row, the decay bump, the spend join rows,
    and the durable event - context, treated like spend. Call it just before the caller's
    commit: `next_seq` and `mark_used` take row locks that live until COMMIT, so nothing slow
    (a provider call above all) may run between here and there. A None pack records nothing.

    **The one choke point for writing `task.context_pack`, so the Project fence lives here too**.
    Planning assembles the pack without a lock and holds it across the planner's
    provider call; a move can commit in that window, and the task planning holds is stale. So
    the task's row is locked first and its Project re-read: a task now in a Project stores (and
    announces, and bumps) only what `fence_for_project` would have left it - a no-op for a pack
    `assemble` built for that Project, which has no `user` lane and only the Project's own rest."""
    if pack is None:
        return
    # NO KEY UPDATE (`Task.hold`'s strength): the lock queues a move and every seq claim, never
    # a child row's insert.
    await session.refresh(task, attribute_names=["project_id"], with_for_update={"key_share": True})
    pack = await Fence.of(task).narrow(session, task, pack)
    if pack is None:
        return
    ids = pack.memory_ids()
    now = utcnow()
    task.context_pack = pack.model_dump(mode="json")
    await Memory.mark_used(session, ids, at=now)
    MemoryUse.record_all(session, ids, task.id)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        ContextAssembled(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=now,
            lanes=lane_views(pack),
            total_tokens=pack.total_tokens,
            memory_ids=[MemoryId(i) for i in ids],
        ),
    )


async def readable_pack(session: AsyncSession, task: Task) -> ContextPack | None:
    """The stored pack as this task may read it now, fenced on read through the same `Fence` as on store;
    the one door every consumer reads it through. None when nothing is left."""
    if not task.context_pack:
        return None
    return await Fence.of(task).narrow(session, task, ContextPack.model_validate(task.context_pack))


def memory_searcher(task: Task) -> SearchMemory | None:
    """`TaskDeps.memory_search` for a frameless run: its creator's own notes matching a query, best
    first, each search on its own short session; None in a Project, and empty once the task moved into one."""
    if not Fence.of(task).admits(PERSONAL):
        return None
    task_id, creator = task.id, task.creator_actor_id

    async def search(query: str) -> list[str]:
        async with row_scope(Task, task_id) as session:
            row = await session.get(Task, task_id)
            if row is None or not Fence.of(row).admits(PERSONAL):  # fail-closed: moved mid-run
                return []
            hits = await Memory.search(
                session, MemoryScope.USER, user_id=creator, query_text=query, limit=SEARCH_LIMIT
            )
            await record_searched(session, row, hits)
            await session.commit()
            return [m.text for m in hits]

    return search


async def record_searched(session: AsyncSession, task: Task, hits: Sequence[Memory]) -> None:
    """What a `memory_search` handed a run, recorded like an assembled pack (the use rows, the decay bump,
    `context.assembled` with a `user` lane, never the query); the creator's lane, so `redact_for` hides it
    from anyone else. The task row first, then its memories, as `record` locks them; the caller commits."""
    if not hits:
        return
    items = [ContextItem(text=m.text, memory_id=m.id, kind=m.kind) for m in hits]
    lane = ContextLane(lane="user", items=items, tokens=sum(TOKENS.count(i.text) for i in items))
    ids = [m.id for m in hits]
    seq = await task.next_seq(session)
    now = utcnow()
    await Memory.mark_used(session, ids, at=now)
    MemoryUse.record_all(session, ids, task.id)
    await TaskEvent.append(
        session,
        ContextAssembled(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=now,
            lanes=lane_views(ContextPack(lanes=[lane], total_tokens=lane.tokens)),
            total_tokens=lane.tokens,
            memory_ids=[MemoryId(i) for i in ids],
        ),
    )


async def fence_for_project(session: AsyncSession, task: Task, project_id: uuid.UUID | None) -> None:
    """The fence's other door: a task moved INTO a Project keeps the pack it froze while it
    was someone's own (or another Project's), and that pack is mounted as /task/context.md for every
    remaining step, inlined for tool-less agents, and reused by replan. Shed what work in
    `project_id` may not read (`Fence.narrow`) - another Project's notes and house rules too, whether
    the task came straight from that Project or via home (a pack outlives a hop through personal).
    The next turn reassembles for the new Project. A move home sheds nothing. In the caller's
    transaction, before its commit. (A step already running at the move may have read the
    creator's own memory - only the creator can move a task, so that was her own act.)"""
    if not task.context_pack:
        return
    pack = ContextPack.model_validate(task.context_pack)
    fenced = await Fence(project_id).narrow(session, task, pack)
    if fenced is not pack:
        task.context_pack = fenced.model_dump(mode="json") if fenced is not None else None


async def _project_bot(session: AsyncSession, task: Task, project_id: uuid.UUID) -> bool:
    """The task's direct bot is a Project bot of this Project - `assemble`'s own carrier test."""
    from prismoid.db.models.bot import BotRecord

    if not task.direct_bot:
        return False
    return bool(
        await BotRecord.all_versions(
            session, task.direct_bot, project_id=project_id, owner_actor_id=task.creator_actor_id
        )
    )


async def _house_rules(session: AsyncSession, project_id: uuid.UUID) -> str | None:
    """The Project's instructions as `assemble` clips them into the `instructions` lane."""
    project = await session.get(Project, project_id)
    if project is None or not project.instructions:
        return None
    return clip_head(project.instructions, BUDGETS["instructions"])[0]


async def _project_owned(session: AsyncSession, lane: ContextLane, project_id: uuid.UUID) -> bool:
    """Every item of the lane is a ledger row carried by this Project (and there is at least one)."""
    ids = {i.memory_id for i in lane.items if i.memory_id is not None}
    if not ids or len(ids) != len(lane.items):
        return False
    rows = await Memory.by_ids(session, list(ids))
    return {m.id for m in rows if m.project_id == project_id} == ids


# (`_thread_lane` - the ≤400-token parent précis - was replaced by the thread transcript:
# the conversation is re-derived whole from the record by `operations/thread.py`,
# mounted as /task/thread.md and inlined in the planner's brief; the lane above is its receipt.
# Reason: a follow-up's agent knew three sentences about the thread the user was continuing.)
