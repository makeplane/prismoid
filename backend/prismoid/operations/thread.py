# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The thread transcript - the conversation, read back out of the record.

A task IS a conversation now: `task.created` opens it, every `task.continued` is the user's next
turn, the step narrations are what the computer said back. This module rebuilds that dialogue
from the append-only durable events so that every agent involved in a continuation - planner,
executors, a fork child's whole lineage - knows the entire thread, not a 400-token précis.

Two halves, `context.py`'s shape: `transcript_from` is pure (unit-testable without a database);
`render_thread` walks the row world - the task's own events, plus the parent chain for forks.
The transcript is **never persisted** (see `schemas/thread.py`): deterministic per (events ≤
boundary, budget), so the planner's brief, the mounted file and the record can never disagree.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.context.blocks import today_section
from prismoid.ai.context.thread import safe_name
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.message import Message
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.errors import NotFound
from prismoid.operations.context import Fence, clip_tail
from prismoid.schemas.events import (
    ArtifactProduced,
    ArtifactRevised,
    DurableEvent,
    EscalationRaised,
    GateOpened,
    GateResolved,
    PlanEdited,
    SteerApplied,
    SteerRejected,
    SteerRetracted,
    StepNarration,
    TaskCancelled,
    TaskContinued,
    TaskCreated,
    TaskDelivered,
    TaskProposed,
    TaskStatusChanged,
)
from prismoid.schemas.ids import TaskId
from prismoid.schemas.plan import GateAction, PlanEditKind, TaskStatus
from prismoid.schemas.steering import NOT_CARRIED
from prismoid.schemas.thread import AuthorRef, EntryKind, ThreadEntry, ThreadTranscript, ThreadTurn
from prismoid.schemas.tokens import TOKENS

# Budgets. The planner reads the conversation inline; executors get the mounted file. Module
# constants, not conf knobs - deterministic tests, and nobody tunes a transcript per deploy.
PLANNER_THREAD_TOKENS = 12_000  # "# The conversation so far" in the planner's brief
MOUNT_THREAD_TOKENS = 16_000  # /task/thread.md ≈ 64 KB at ~4 chars/token
EXPRESS_THREAD_TOKENS = 8_000  # the Lead's inline thread - a breath, not a mount
THREAD_MD_MAX_BYTES = 64_000  # the hard byte lip on the rendered file (belt; the budget sizes it)
MAX_HOPS = 8  # legacy multi-hop follow-up chains; a fork walks exactly one hop
_ELISION_RESERVE = 24  # tokens held back for the honest "[N earlier turns elided]" line
# The conversation a chat-born task came from rides the thread as turn 0: the
# newest lines up to the message that minted the task, bounded like the reply lane's own read.
PRELUDE_ROWS = 40
PRELUDE_CHARS = 24_000

# The conversation-shaped events. Everything else (tool journals, usage, step states, context
# spend, snapshots, renames, moves) is machinery - the record keeps it; the dialogue doesn't.
TRANSCRIPT_EVENT_TYPES: tuple[str, ...] = (
    "task.created",
    "task.continued",
    "task.proposed",
    "gate.opened",
    "gate.resolved",
    "plan.edited",
    "steer.applied",
    "steer.retracted",
    "steer.rejected",
    "step.narration",
    "artifact.produced",
    "artifact.revised",
    "task.delivered",
    "task.status",
    "task.cancelled",
    "escalation.raised",
)

# ── the pure half ─────────────────────────────────────────────────────────────────────────────


def _squash(text: str) -> str:
    return "".join(text.split())


def _author(ev: DurableEvent) -> AuthorRef | None:
    return AuthorRef(id=ev.actor.id, name=ev.actor.name) if ev.actor is not None else None


def _asker(ev: DurableEvent) -> str | None:
    """The name of the HUMAN whose ask this is - an api key or an agent is nobody to address."""
    return ev.actor.name if ev.actor is not None and ev.actor.type == "user" else None


def _entry(
    kind: EntryKind,
    text: str,
    task_id: TaskId,
    turn: int,
    seq: int,
    author: AuthorRef | None = None,
) -> ThreadEntry:
    return ThreadEntry(
        kind=kind,
        text=text,
        task_id=task_id,
        turn=turn,
        seq=seq,
        tokens=TOKENS.count(text),
        author=author,
    )


def _answer_text(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def transcript_from(
    chain: list[tuple[TaskId, list[DurableEvent]]],
    *,
    budget_tokens: int,
    prelude: Sequence[ThreadEntry] = (),
) -> ThreadTranscript:
    """Events to dialogue, deterministically (`chain` root-first; `prelude` is turn 0 of the root), clipped
    under `budget_tokens`: newest turns whole, the opening ask kept, every user entry kept."""
    walk = _Walk(chain)
    if prelude and chain:
        walk.turns.append(ThreadTurn(task_id=chain[0][0], turn=0, entries=list(prelude)))
    for task_id, events in chain:
        walk.task_id, walk.goal = task_id, None
        for ev in events:
            handler = next((h for kind, h in _HANDLERS if isinstance(ev, kind)), None)
            if handler is not None:
                handler(walk, ev)
        walk.prev_goal = walk.goal if walk.goal is not None else walk.prev_goal
    walk.flush()
    return _fit(walk.turns, budget_tokens, walk.current_ask, walk.current_author)


class _Walk:
    """The walk over one chain's events: the turns so far, the turn being written, and what the
    rules need to remember across events (gate questions, the turn's narrations, the asker)."""

    def __init__(self, chain: list[tuple[TaskId, list[DurableEvent]]]) -> None:
        self.turns: list[ThreadTurn] = []
        self.entries: list[ThreadEntry] = []  # the current turn's
        self.current: tuple[TaskId, int] | None = None
        self.questions: dict[str, dict[str, str]] = {}  # gate_id → {qid: prompt}
        self.narration_squash = ""  # the current turn's narrations, squashed, for delivered-dedupe
        self.prev_goal: str | None = None  # the previous task-in-chain's opening, for fork suppression
        self.current_ask: str | None = None
        self.current_author: str | None = None
        self.task_id: TaskId | None = None  # the chain task whose events are being walked
        self.goal: str | None = None  # that task's own opening, once seen
        # command_id → how a steer that never applied settled (paired with its `plan.edited{steer}`)
        self.unapplied: dict[str, SteerRetracted | SteerRejected] = {
            str(ev.command_id): ev
            for _, events in chain
            for ev in events
            if isinstance(ev, SteerRetracted | SteerRejected)
        }
        self.applied = {
            str(ev.command_id) for _, events in chain for ev in events if isinstance(ev, SteerApplied)
        }
        self.tip = chain[-1][0] if chain else None

    @property
    def at(self) -> TaskId:
        assert self.task_id is not None  # set before any event is walked
        return self.task_id

    def flush(self) -> None:
        if self.current is not None and self.entries:
            self.turns.append(ThreadTurn(task_id=self.current[0], turn=self.current[1], entries=self.entries))
        self.entries = []
        self.narration_squash = ""

    def open_turn(self, turn: int) -> int:
        """Make `turn` of the walked task the current one (flushing the last) and return it."""
        if self.current != (self.at, turn):
            self.flush()
            self.current = (self.at, turn)
        return turn

    def same_turn(self) -> int:
        """The walked task's current turn - turn 1 if the walk has not reached it yet."""
        return self.open_turn(self.current[1] if self.current and self.current[0] == self.at else 1)

    def add(self, kind: EntryKind, text: str, turn: int, ev: DurableEvent, *, by: bool = False) -> None:
        """One entry in the current turn; `by` attributes it to the event's actor."""
        self.entries.append(_entry(kind, text, self.at, turn, ev.seq, author=_author(ev) if by else None))


def _on_created(walk: _Walk, ev: TaskCreated) -> None:
    walk.goal = ev.goal
    walk.open_turn(1)
    if walk.prev_goal is not None and _squash(ev.goal) == _squash(walk.prev_goal):
        walk.add("event", "[the user forked the conversation here]", 1, ev)
        return
    walk.add("user", ev.goal, 1, ev, by=True)
    walk.current_ask, walk.current_author = ev.goal, _asker(ev)


def _on_continued(walk: _Walk, ev: TaskContinued) -> None:
    walk.add("user", ev.message, walk.open_turn(ev.turn), ev, by=True)
    walk.current_ask, walk.current_author = ev.message, _asker(ev)


def _on_plan_edited(walk: _Walk, ev: PlanEdited) -> None:
    t = walk.same_turn()
    if ev.edit.kind is PlanEditKind.STEER:
        _on_steer(walk, ev, t)
    elif ev.edit.kind is PlanEditKind.REPLAN:
        walk.add("event", f"[replanned: {ev.edit.reason[:200]}]", t, ev, by=True)
    elif ev.edit.kind is PlanEditKind.HATCH:
        walk.add("event", f"[the reply escalated to a plan: {ev.edit.reason[:200]}]", t, ev, by=True)
    elif ev.edit.kind is PlanEditKind.LEAD:
        walk.add("event", f"[planned: {ev.edit.reason[:200]}]", t, ev, by=True)


def _on_steer(walk: _Walk, ev: PlanEdited, t: int) -> None:
    """Retracted: its author withdrew it before anything took it - no line at all. An ancestor's
    steer still open at the fork's bound was that task's run's."""
    command = ev.edit.command_id
    settled = walk.unapplied.get(command or "")
    open_at_fork = walk.at != walk.tip and command is not None and command not in walk.applied
    if settled is None and not open_at_fork:
        walk.add("steering", ev.edit.reason, t, ev, by=True)
    elif not isinstance(settled, SteerRetracted):
        marker = f"not applied: {settled.reason}" if settled else f"not carried: {NOT_CARRIED}"
        who = safe_name(ev.actor.name if ev.actor is not None else "")
        walk.add("event", f"[{marker}] {who} steered: {ev.edit.reason}", t, ev, by=True)


def _on_proposed(walk: _Walk, ev: TaskProposed) -> None:
    kind = f" ({ev.deliverable})" if ev.deliverable else ""
    walk.add(
        "event",
        f"[the computer proposed a task{kind}: {ev.goal[:200]} - waiting for the user to start it]",
        walk.same_turn(),
        ev,
    )


def _on_gate_opened(walk: _Walk, ev: GateOpened) -> None:
    if ev.questions is not None:
        walk.questions[str(ev.gate_id)] = {q.id: q.prompt for q in ev.questions.questions}


def _on_gate_resolved(walk: _Walk, ev: GateResolved) -> None:
    t = walk.same_turn()
    asked = walk.questions.get(str(ev.gate_id), {})
    said = f"{safe_name(ev.actor.name)} answered" if ev.actor is not None else "They said"
    if ev.action is GateAction.ACCEPT:
        content = ev.content or {}
    elif ev.action is GateAction.DECLINE:
        walk.add("event", "[skipped the questions - defaults used]", t, ev)
        content = {qid: v for qid, v in (ev.content or {}).items() if qid in asked}
    else:
        return
    # A skipped question is told as its default, never as the person's answer. Unrecorded (the
    # clock, older events): a decline's content was all defaults, an accept's all answers.
    if ev.defaulted is not None:
        skipped = set(ev.defaulted)
    else:
        skipped = set(content) if ev.action is GateAction.DECLINE else set()
    for qid, value in content.items():
        prompt = asked.get(qid, qid)
        if qid in skipped:
            walk.add("qa", f"**Asked:** {prompt} **Default used:** {_answer_text(value)}", t, ev)
        else:
            walk.add("qa", f"**Asked:** {prompt} **{said}:** {_answer_text(value)}", t, ev, by=True)


def _on_narration(walk: _Walk, ev: StepNarration) -> None:
    walk.add("assistant", ev.markdown, walk.same_turn(), ev)
    walk.narration_squash += _squash(ev.markdown)


def _on_produced(walk: _Walk, ev: ArtifactProduced) -> None:
    walk.add("event", f"[published {ev.title}]", walk.same_turn(), ev)


def _on_revised(walk: _Walk, ev: ArtifactRevised) -> None:
    walk.add("event", f"[revised {ev.title} - revision {ev.revision}]", walk.same_turn(), ev)


def _on_delivered(walk: _Walk, ev: TaskDelivered) -> None:
    t = walk.same_turn()
    summary = ev.summary.strip()
    if summary and _squash(summary) not in walk.narration_squash:
        walk.add("assistant", summary, t, ev)
    walk.add("event", "[delivered]", t, ev)


def _on_status(walk: _Walk, ev: TaskStatusChanged) -> None:
    if ev.status is TaskStatus.FAILED:
        reason = f": {ev.reason[:200]}" if ev.reason else ""
        walk.add("event", f"[the task failed{reason}]", walk.same_turn(), ev)
    elif ev.status is TaskStatus.CANCELLED and ev.summary:
        walk.add("event", f"[stopped - {ev.summary[:600]}]", walk.same_turn(), ev)


def _on_cancelled(walk: _Walk, ev: TaskCancelled) -> None:
    walk.add("event", "[cancelled]", walk.same_turn(), ev)


def _on_escalation(walk: _Walk, ev: EscalationRaised) -> None:
    t = walk.same_turn()
    why = ev.diagnosis.why.splitlines()[0] if ev.diagnosis.why else ""
    walk.add("event", f"[stopped and escalated: {ev.diagnosis.what_failed} - {why}]"[:400], t, ev)


# One handler per conversation-shaped event, tried in order (an event matches one class).
_HANDLERS: tuple[tuple[type[Any], Callable[[_Walk, Any], None]], ...] = (
    (TaskCreated, _on_created),
    (TaskContinued, _on_continued),
    (PlanEdited, _on_plan_edited),
    (TaskProposed, _on_proposed),
    (GateOpened, _on_gate_opened),
    (GateResolved, _on_gate_resolved),
    (StepNarration, _on_narration),
    (ArtifactProduced, _on_produced),
    (ArtifactRevised, _on_revised),
    (TaskDelivered, _on_delivered),
    (TaskStatusChanged, _on_status),
    (TaskCancelled, _on_cancelled),
    (EscalationRaised, _on_escalation),
)


def _fit(
    turns: list[ThreadTurn], budget: int, current_ask: str | None, current_author: str | None = None
) -> ThreadTranscript:
    """The deterministic clip: newest turns whole, the opening ask always, honesty about the rest."""
    total = _tokens(turns)
    if total <= budget or not turns:
        return ThreadTranscript(
            turns=turns,
            elided_turns=0,
            truncated=False,
            total_tokens=total,
            budget_tokens=budget,
            current_ask=current_ask,
            current_author=current_author,
        )

    # The anchor is the task's own opening ask - never a conversation prelude (turn 0), whose
    # lines are context the budget may drop first.
    first = next((t for t in turns if t.turn >= 1), turns[0])
    anchor_entries = [e for e in first.entries if e.kind == "user"]
    reserve = sum(e.tokens for e in anchor_entries) + _ELISION_RESERVE
    kept = _keep_newest(turns, max(budget - reserve, 0))

    # A dropped prelude (turn 0) is context, not a turn: the honest elision line counts turns.
    elided = sum(1 for t in turns if t.turn >= 1 and t not in kept)
    result = kept
    if kept and first not in kept and anchor_entries:
        result = [ThreadTurn(task_id=first.task_id, turn=first.turn, entries=anchor_entries), *kept]

    return ThreadTranscript(
        turns=result,
        elided_turns=elided,
        truncated=True,
        total_tokens=_tokens(result),
        budget_tokens=budget,
        current_ask=current_ask,
        current_author=current_author,
    )


def _tokens(turns: list[ThreadTurn]) -> int:
    return sum(e.tokens for t in turns for e in t.entries)


def _keep_newest(turns: list[ThreadTurn], room: int) -> list[ThreadTurn]:
    """The newest turns that fit `room` whole, oldest first. When even the newest turn alone is
    over, it is kept clipped (`_clip_turn`) and older turns join it while they still fit."""
    kept: list[ThreadTurn] = []
    used = 0
    for turn in reversed(turns):
        cost = sum(e.tokens for e in turn.entries)
        if kept and used + cost > room:
            break
        if not kept and cost > room:
            clipped = _clip_turn(turn, room)
            kept = [clipped]
            used = sum(e.tokens for e in clipped.entries)
            continue
        kept.insert(0, turn)
        used += cost
    return kept


def _clip_turn(turn: ThreadTurn, room: int) -> ThreadTurn:
    """An over-budget turn cut to `room`: drop its non-user entries oldest-first, then clip the
    last oversized survivors' tails. The user's words are never dropped."""
    survivors = list(turn.entries)
    while sum(e.tokens for e in survivors) > room and any(e.kind != "user" for e in survivors):
        drop = next(i for i, e in enumerate(survivors) if e.kind != "user")
        del survivors[drop]
    clipped: list[ThreadEntry] = []
    for e in survivors:
        if e.kind != "user" and sum(x.tokens for x in clipped) + e.tokens > room:
            text, tokens, _ = clip_tail(e.text, max(room - sum(x.tokens for x in clipped), 40))
            e = e.model_copy(update={"text": text, "tokens": tokens})
        clipped.append(e)
    return ThreadTurn(task_id=turn.task_id, turn=turn.turn, entries=clipped)


# ── the row-world half ────────────────────────────────────────────────────────────────────────


async def render_thread(
    session: AsyncSession,
    task: Task,
    *,
    budget_tokens: int,
    until_seq: int | None = None,
) -> ThreadTranscript:
    """The task's conversation, parents first for forks, tip last - bounded and budgeted.

    `until_seq` bounds the TIP task's events (the planning boundary): the executor mounts
    exactly the history the planner saw, byte for byte, no matter what lands mid-turn. Each
    ancestor is read up to its fork point (`forked_from`) - a parent keeps getting turns after
    the fork, and those are the other branch. The walk is cycle-guarded and hop-capped; a fork's
    parent contains its whole thread in its own events, so one hop is the normal case and legacy
    follow-up chains are the long one.
    """
    lineage = [(task, until_seq), *await ancestry(session, task)]
    lineage.reverse()  # root first

    chain: list[tuple[TaskId, list[DurableEvent]]] = []
    for node, bound in lineage:
        events = await TaskEvent.of_types(session, node.id, TRANSCRIPT_EVENT_TYPES, until=bound)
        chain.append((TaskId(node.id), events))

    # A chat-born root: the talk that led here opens the thread as turn 0 - read
    # through the fence of the task being RENDERED, which for a fork is not the root.
    prelude = await conversation_prelude(session, lineage[0][0], fence=Fence.of(task))
    return transcript_from(chain, budget_tokens=budget_tokens, prelude=prelude)


async def ancestry(session: AsyncSession, task: Task) -> list[tuple[Task, int | None]]:
    """What `task`'s thread reads of the tasks it was forked from: each ancestor, nearest first,
    with the seq it was forked at (`forked_from`, as the task's creator may still read it). The
    walk is cycle-guarded and capped at MAX_HOPS; it stops at the root, or at an ancestor the
    creator can no longer open. Empty for a task that is not a fork (no query)."""
    found: list[tuple[Task, int | None]] = []
    seen: set[uuid.UUID] = {task.id}
    cur = task
    while len(found) < MAX_HOPS:
        fork = await forked_from(session, cur, reader_id=task.creator_actor_id)
        if fork is None or fork[0].id in seen:
            break  # the root, an ancestor the tip's creator cannot open, or a cycle (corruption)
        found.append(fork)
        seen.add(fork[0].id)
        cur = fork[0]
    return found


FORKED = "forked"  # `teammate_word`'s answer when the word is in what a fork carries, not its own record


async def teammate_word(session: AsyncSession, task: Task) -> str | None:
    """The event type by which a person other than the creator put words into this task's thread, `FORKED`
    when only an ancestor carries it, else None: the one answer for connector borrow and memory stand-down."""
    own = await TaskEvent.teammate_input(session, task.id, task.creator_actor_id)
    if own is not None:
        return own
    for parent, bound in await ancestry(session, task):
        if await TaskEvent.teammate_input(
            session, parent.id, task.creator_actor_id, until=bound, with_created=True
        ):
            return FORKED
    return None


async def forked_from(
    session: AsyncSession, child: Task, *, reader_id: uuid.UUID
) -> tuple[Task, int | None] | None:
    """The task `child` branched from, as `reader_id` may still read it: the parent
    and the last of its seqs the fork was made at (`task.created.parent_seq`; None for a fork made
    before the bound, read whole). None when `child` has no parent, or when the reader can no
    longer open it - a parent taken home, or one that was never shared. The one answer to "what
    of its parent may a fork read", for its transcript and its inherited files alike."""
    if child.parent_task_id is None:
        return None
    reader = await session.get(Actor, reader_id)
    if reader is None:
        return None
    try:
        parent = await Task.get_for(session, child.parent_task_id, reader)
    except NotFound:
        return None
    created = await TaskEvent.latest_of_type(session, child.id, "task.created")
    return parent, created.parent_seq if isinstance(created, TaskCreated) else None


async def conversation_prelude(session: AsyncSession, task: Task, *, fence: Fence) -> list[ThreadEntry]:
    """The conversation a chat-born task came from, as thread entries: the newest
    `PRELUDE_ROWS` lines up to and including the message that minted the task, clipped to
    `PRELUDE_CHARS` oldest-first. Deterministic per task: message rows are append-only and the
    bound is the row's own `origin_message_seq`. Empty for every other task.

    Private content, so read through `fence`: a personal conversation is its
    owner's, its bot's lines written from her private memory, and work in a Project reads only a
    conversation of that Project - whichever task came from the chat, and wherever it is now."""
    if task.origin_conversation_id is None or task.origin_message_seq is None:
        return []
    conv = await session.get(Conversation, task.origin_conversation_id)
    if conv is None or not fence.admits(conv.project_id):
        return []
    rows = list(
        reversed(
            await Message.before(
                session, task.origin_conversation_id, task.origin_message_seq, limit=PRELUDE_ROWS
            )
        )
    )
    # The minting line IS the task's own opening ask when the gesture carried it verbatim
    # (`/task <text>`); turn 1 renders it, so turn 0 does not repeat it.
    if rows and rows[-1].role == "user" and " ".join(rows[-1].text.split()) == " ".join(task.goal.split()):
        rows.pop()
    entries: list[ThreadEntry] = []
    for m in rows:
        if m.role == "user":
            who = f"{m.sender}: " if m.sender and m.sender != "owner" else ""
            kind, text = "user", f"{who}{m.text}"
        elif m.role == "bot":
            kind, text = "assistant", m.text
            if m.proposal:
                text += f"\n\n[proposed a task: {str(m.proposal.get('goal', ''))[:200]}]"
        else:
            kind, text = "event", f"[{m.kind}] {m.text}"
        entries.append(_entry(kind, text, TaskId(task.id), 0, m.seq))  # type: ignore[arg-type]
    total = 0
    kept: list[ThreadEntry] = []
    for e in reversed(entries):  # newest first, so the clip drops the OLDEST lines
        if total + len(e.text) > PRELUDE_CHARS:
            break
        total += len(e.text)
        kept.append(e)
    kept.reverse()
    return kept


async def author_timezone(session: AsyncSession, task: Task) -> str | None:
    """The timezone of the turn's author - a teammate's turn is the teammate's clock."""
    user = await session.get(User, task.turn_actor_id or task.creator_actor_id)
    return user.timezone if user else None


async def author_today(session: AsyncSession, task: Task) -> str:
    """`blocks.today_section` for the turn's author: the planner and every step brief read "next
    Friday" against it; with no date in the brief a model guesses the month and year."""
    return today_section(await author_timezone(session, task), utcnow())
