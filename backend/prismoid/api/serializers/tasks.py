# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every task shape, one file. `*In` = request, `*Out` = response.

That two-suffix convention answers "which serializer does GET vs POST use" by making the question
unaskable - the function signature says it: `create_task(body: TaskCreateIn) -> TaskAcceptedOut`.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from prismoid.api import links
from prismoid.api.serializers.machines import MachineOut
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.schemas.inputs import MAX_INPUT_BYTES, MAX_INPUTS, a_name_not_a_path
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import PlanView
from prismoid.schemas.sizing import normalize, size_marker
from prismoid.schemas.steering import DIRECTIVE_MAX
from prismoid.schemas.usage import Budget

# The base64 of the largest attachment, plus room for a client that wraps its lines (a newline
# every 76 characters adds about 1/57).
MAX_INPUT_BASE64_CHARS = 4 * math.ceil(MAX_INPUT_BYTES / 3) + MAX_INPUT_BYTES // 32


class TaskInputIn(BaseModel):
    """An attachment on the way in. `content` is base64 - of the **bytes**, not of the text.

    Base64-in-JSON is honest for the few-megabyte documents this is for, and dishonest for a
    200 MB video: it inflates by a third and it holds the whole thing in memory twice. When that
    day comes the answer is a multipart upload endpoint returning a handle, and this shape becomes
    `{filename, handle}` - the *schema* is already right, only the transport changes. Saying so
    here is cheaper than discovering it at 3 a.m.
    """

    filename: str = Field(min_length=1, max_length=120)
    content: str = Field(description="base64 of the file's bytes", max_length=MAX_INPUT_BASE64_CHARS)
    media_type: str = Field(default="text/plain", max_length=100)

    @field_validator("filename")
    @classmethod
    def _is_a_name_not_a_path(cls, value: str) -> str:
        """**The same validator as `RawInput`, and it must be on both.**

        It was only on the inner value - so `../../etc/passwd` sailed through the request schema,
        blew up building `RawInput` inside the endpoint, and came back as an **HTTP 500**. A
        rejected attack that returns a server error is still a bug: wrong status, and a stack trace
        for the attacker's trouble. Validating at the request boundary makes it the 422 it always
        was. The inner check stays - the serializer is the door, not the lock.
        """
        return a_name_not_a_path(value)


class TaskDials(TypedDict):
    """`create_task`'s dials under their wire names: its asking, its cap, what it delivers and where it runs."""

    clarify: str
    budget: dict[str, Any] | None
    deliverable: str | None
    deliverable_format: str | None
    quality: str | None
    scale: str
    target_pages: int | None
    folder: str | None
    docker: bool


class TaskCreateIn(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    project_id: UUID | None = None
    mode: Literal["standard", "fleet", "council"] = (
        "standard"  # the middle value is current; the last is a legacy wire value
    )
    clarify: Literal["auto", "never", "always"] = "auto"  # one intake round max
    budget: Budget | None = None

    # The sources. They land read-only at `/task/inputs/` and become the grounding set every
    # deterministic check re-derives from.
    inputs: list[TaskInputIn] = Field(default_factory=list, max_length=MAX_INPUTS)

    # Skills to pin on this task's steps: refs like "report" (the published head) or
    # "report@3" (an exact version). Resolved to a concrete version at submit time; a bad ref is a
    # 404/422 on submit, not a surprise mid-run. `report@draft` (author-only) resolves only for its author.
    skills: list[str] = Field(default_factory=list, max_length=8)

    # Connectors to mount on this task's steps: refs like "gmail" (the slug) or a row
    # id. Resolved and validated at submit - unknown or not-yet-authorized is a 422 naming the
    # fix ("run authorize"), never a mid-run surprise. Explicit per task, always.
    connectors: list[str] = Field(default_factory=list, max_length=8)

    # Bots to put on this task's roster: refs like "contract-reviewer" (the published
    # head), "contract-reviewer@3", or "contract-reviewer@draft" (author-only). **An override,
    # not the only way in** - the roster is auto-included from the task's Project and the creator's
    # own published bots, because a roster you must remember to attach is not a roster. Name bots
    # here to pin a draft, or to narrow a crowded space to the two that matter.
    bots: list[str] = Field(default_factory=list, max_length=8)

    # Direct mode: with exactly one bot named, that bot answers every turn of this
    # task deterministically - the home dial's "this task is FOR this bot".
    direct: bool = False

    # Fleet seats: catalog refs like "anthropic:claude-haiku-4-5". Only meaningful
    # with mode="fleet" (422 otherwise); empty = every eligible enabled model. Validated
    # against the enabled catalog at submit - a bad seat fails the door, not the run.
    models: list[str] = Field(default_factory=list, max_length=16)

    # The chosen artifact kind. None = Auto - the planner reads the goal. Set = a
    # CONSTRAINT: the final publishing step must be an agent that publishes this kind, the
    # fallback floor becomes that agent, and the kind's skill is auto-pinned. Validated at the
    # door against what the roster can actually publish (so no webapp, no legacy values).
    deliverable: Literal["document", "collection", "spreadsheet", "deck", "code"] | None = None
    # The document's file format: documents only - the door 422s the
    # pair with any other kind; set alone it IMPLIES deliverable="document".
    deliverable_format: Literal["md", "docx", "pdf", "html"] | None = None

    # The requirement dial. None = balanced (the routing table's order, untouched).
    # economy/max re-rank only the unchained tail of executor chains by price; the judge lane
    # is exempt. A dial, not a model name - Rule 7 holds at the door too.
    quality: Literal["max", "balanced", "economy"] | None = None

    # THE SIZE DIAL: five buckets, one vocabulary for every artifact kind -
    # brief · standard · extended · long · book - each a target band + a HARD ceiling the
    # system never pushes past. `auto` (the default) resolves deterministically at planning:
    # a literal number in the goal ("15 pages", "25 slides") picks the smallest covering
    # bucket; otherwise standard, always - adjectives never upgrade (the no-push law).
    # `target_pages` is the target in the KIND'S OWN UNIT (pages, slides, sheets, rows);
    # the name predates the universal dial and stays for wire compat.
    scale: Literal["auto", "brief", "standard", "extended", "long", "book"] = "auto"
    target_pages: int | None = Field(default=None, ge=1, le=400)

    # The duplicate-task guardrail: a client-minted token per send gesture.
    # A replayed token (same creator) returns the EXISTING task - double-fires, HTTP
    # retries, and script reruns can never mint a twin.
    client_token: str | None = Field(default=None, max_length=64)

    # A task on the device: the folder it works in, granted read-write -
    # an absolute path on this computer, checked at the door (`folder_refused`), free of any other
    # live task (`folder_busy`). None: a fresh, empty work folder. `docker` grants it Docker.
    # Both are refused for a task on a cloud Machine.
    folder: str | None = Field(default=None, min_length=1, max_length=1024)
    docker: bool = False

    def dials(self) -> TaskDials:
        return TaskDials(
            clarify=self.clarify,
            budget=self.budget.model_dump(mode="json") if self.budget else None,
            deliverable=self.deliverable,
            deliverable_format=self.deliverable_format,
            quality=self.quality,
            scale=self.scale,
            target_pages=self.target_pages,
            folder=self.folder,
            docker=self.docker,
        )

    @model_validator(mode="after")
    def _fleet_is_single_breath(self) -> TaskCreateIn:
        """ADR-029: a fleet seat answers in one breath (one model call + its verify rung) -
        the explicit big buckets are refused here ("council" is the pre-rename fleet wire
        value and obeys the same law, review); an `auto` that RESOLVES big is clamped to
        standard at planning by the same predicate (`sizing.fleet_allows`), stamped honestly."""
        if self.mode in ("fleet", "council") and self.scale in ("extended", "long", "book"):
            raise ValueError("fleet answers are single-breath - choose brief or standard (or drop the size)")
        return self


class TaskUpdateIn(BaseModel):
    """The user-space surface of a task: its label and its pin. The goal is immutable (it is
    what was asked) and the status is the machine's - neither is editable here."""

    # 60 is the lip everywhere a title lives: breadcrumb, sidebar row, board card.
    title: str | None = Field(default=None, min_length=1, max_length=60)
    pinned: bool | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> TaskUpdateIn:
        if self.title is None and self.pinned is None:
            raise ValueError("send title and/or pinned - an empty update changes nothing")
        return self


class FollowUpDials(TypedDict):
    """`continue_task`'s dials under their wire names: what this turn changes on the thread."""

    clarify: str
    budget: dict[str, Any] | None
    deliverable: str | None
    deliverable_format: str | None
    quality: str | None
    scale: str | None
    target_pages: int | None


class FollowUpIn(BaseModel):
    """A follow-up **continues the SAME task** - the thread is the task: the message
    becomes the next turn on the same row, same record, same stream, with the whole
    conversation in the agent's hands. Never a new task; `fork` is the branching action.

    It still takes the fresh-turn dials: new attachments append (a filename may not shadow an
    earlier turn's), skills merge, budget/models/deliverable update when given. `project_id` and
    `mode` are thread-DNA - inherited, never chosen. A live task refuses with 409 ("steer it
    instead"); the client_token makes a replayed send gesture return the same turn, never two."""

    goal: str = Field(min_length=1, max_length=8000)
    clarify: Literal["auto", "never", "always"] = "auto"
    budget: Budget | None = None
    inputs: list[TaskInputIn] = Field(default_factory=list, max_length=MAX_INPUTS)
    skills: list[str] = Field(default_factory=list, max_length=8)
    connectors: list[str] = Field(default_factory=list, max_length=8)  # merge by id
    bots: list[str] = Field(default_factory=list, max_length=8)  # supersede by name
    models: list[str] = Field(default_factory=list, max_length=16)
    deliverable: Literal["document", "collection", "spreadsheet", "deck", "code"] | None = None
    # The document's file format: documents only - the door 422s the
    # pair with any other kind; set alone it IMPLIES deliverable="document".
    deliverable_format: Literal["md", "docx", "pdf", "html"] | None = None
    quality: Literal["max", "balanced", "economy"] | None = None  # updates when given (fresh-turn dial)
    # None = keep the thread's size; else update (the five buckets + auto re-resolve)
    scale: Literal["auto", "brief", "standard", "extended", "long", "book"] | None = None
    target_pages: int | None = Field(default=None, ge=1, le=400)
    client_token: str | None = Field(default=None, max_length=64)  # same guardrail as create

    def dials(self) -> FollowUpDials:
        return FollowUpDials(
            clarify=self.clarify,
            budget=self.budget.model_dump(mode="json") if self.budget else None,
            deliverable=self.deliverable,
            deliverable_format=self.deliverable_format,
            quality=self.quality,
            scale=self.scale,
            target_pages=self.target_pages,
        )


class RetryIn(BaseModel):
    """The Retry button: run the latest ask again as a new turn, whole thread in
    hand. Only the duplicate-guardrail token rides along - the ask is the thread's own."""

    client_token: str | None = Field(default=None, max_length=64)


class FeedbackIn(BaseModel):
    """The thumbs. One press, one durable `task.feedback` frame; the newest is the
    turn's current mood. Never machine state - grading an answer moves nothing."""

    rating: Literal["up", "down"]


class ForkIn(BaseModel):
    """One-click fork - THE branching action: a NEW task on the parent's thread that
    carries the whole conversation and the parent's dials (budget, seats, deliverable, skill
    pins), born HELD: it sits with a composer until the first directive launches it. No goal,
    no dials of its own - only the duplicate-guardrail token rides along, and, for a fork of a task
    on the device, the folder it works in (it never copies the parent's)."""

    client_token: str | None = Field(default=None, max_length=64)
    folder: str | None = Field(default=None, min_length=1, max_length=1024)


class TaskAcceptedOut(BaseModel):
    task_id: UUID
    stream_url: str
    events_url: str  # the SSE twin - `curl -N` is the canonical first look at any task
    turn: int = 1  # which conversation turn this acceptance opened
    # The record's high-water seq at accept: `events --since last_seq-1` replays from exactly
    # this gesture's frame - what `--wait` uses so a continued task's earlier terminal frames
    # never end the follow early.
    last_seq: int = 0


class CostSummaryOut(BaseModel):
    total_usd: Decimal = Decimal("0")
    calls: int = 0


class TaskCountsOut(BaseModel):
    """Visible-task tallies by machine status. The client folds them into its product groups -
    the server counts states, it does not invent groupings."""

    counts: dict[str, int]


class TaskSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    goal: str
    title: str | None  # the creator's label; NULL = derive from the goal
    pinned_at: dt.datetime | None  # user-space triage; NULL = unpinned
    archived_at: dt.datetime | None  # the pin's sibling; NULL = active (default lists)
    held_at: dt.datetime | None  # the fork's hold; NULL = normal lifecycle (user law v4)
    status: str
    mode: str
    project_id: UUID | None
    turn: int  # the conversation counter, 1-based - a follow-up bumps it in place
    created_at: dt.datetime
    # THE RESOLVED SIZE: `scale` is one of the five buckets once planning has
    # resolved an `auto` - the read-back that lets every surface show which size the task
    # actually became. `target_pages` is the target in the kind's own unit; `deliverable` is
    # the forced kind when one was chosen (the marker's unit source).
    scale: str = "auto"
    target_pages: int | None = None
    deliverable: str | None = None
    deliverable_format: str | None = None  # md|docx|pdf|html; documents only
    # WHO chose the kind: "user" | "auto" | "plan" | null (unresolved). The web's
    # chip composes its label client-side (kit/artifact-icon is the one vocabulary home -
    # unlike size_marker, a kind has no unit math for the server to own).
    deliverable_source: str | None = None
    # The DM lane's keeper: the ONE bot that answers this thread, or None. The
    # weave was write-only until now - the page can finally say "Direct · reviewer".
    direct_bot: str | None = None
    # Who is on it (the Project lobby's Live now): the creator and the author of the
    # CURRENT turn, as people. The ids are the row's own (user ids, presented as users) and ride on
    # every read: two people can share a name, so "is it someone else's turn?" compares ids. The
    # NAMES the list fills - one batched lookup per page, never one per row - and they are null
    # when the person cannot be named (no profile row). The detail read names the creator only
    # (one keyed read: a teammate's approval dock says who it waits on); it names turn authors
    # through the record's events.
    creator_id: UUID | None = None
    turn_author_id: UUID | None = None  # a NULL turn author reads as the creator
    creator_name: str | None = None
    turn_author_name: str | None = None
    # The settled turn's typed ending, read off its newest settle frame: done | partial | could_not
    # (an honest could-not reads apart from a fault). Null while live, and on settles that carry none.
    ending: str | None = None

    @computed_field
    @property
    def size_marker(self) -> str | None:
        """The server-composed read-back: "extended · ~15 pages" - the ONE string
        the CLI and the web chip print verbatim, so no client ever re-derives the kind's
        unit. None until an `auto` resolves: the chip hides, the CLI prints nothing."""
        bucket = normalize(self.scale)
        if bucket is None:
            return None
        try:
            kind = ArtifactType(self.deliverable) if self.deliverable else None
        except ValueError:
            kind = None  # a legacy or foreign kind string reads as document semantics
        return size_marker(kind, bucket, self.target_pages)


class TaskInputOut(BaseModel):
    """An attachment as the transcript shows it - name, size, kind, and a presigned
    download link (the chat's preview). The sha stays internal: provenance lives in the
    verification receipt, not the chat."""

    filename: str
    bytes: int
    media_type: str
    url: str


class BotPinOut(BaseModel):
    """One bot on the task's roster override: the name, and the version the pin
    resolved to (None = the head at plan time)."""

    name: str
    version: int | None = None


class TaskDetailOut(TaskSummaryOut):
    plan: PlanView
    cost: CostSummaryOut
    criteria: dict[str, Any]
    # The fork's provenance: set on a forked task, so the page can load the parent
    # thread at mount instead of waiting for the SSE `task.created` frame. NULL on roots.
    parent_task_id: UUID | None = None
    inputs: list[TaskInputOut] = Field(default_factory=list)
    # The rest of the weave's read path: which bots were pinned, and whether a
    # routine minted this task. Provenance the columns always held and the wire never spoke.
    bot_pins: list[BotPinOut] = Field(default_factory=list)
    origin_routine_id: UUID | None = None
    # Chat provenance: the conversation this task was started from, and the
    # message that started it - the page says "from a chat with {bot}".
    origin_conversation_id: UUID | None = None
    origin_message_seq: int | None = None
    can_edit: bool = False  # may the caller follow up, steer, answer (the composer's viewer mode)
    # May the caller archive/delete it (the creator, or the owner of its Project), and is the
    # caller its creator (the only one who renames or pins) - so the ⋯ menu offers no refused verb.
    can_curate: bool = False
    is_creator: bool = False
    machine: MachineOut | None = None  # the task's remembered sandbox, null until a step wakes one
    plan_epoch: int = 0  # the epoch a steer typed now is issued against (`issued_epoch`)
    # May the caller end ANYONE's control of the browser - the Project owner, or a personal
    # task's creator - so the screen offers "End {name}'s control" only where the server allows it.
    can_end_control: bool = False
    # A step waiting for its device: "Waiting for <device name>" (or
    # "your computer") while its device-away gate is open. Null otherwise.
    waiting_for: str | None = None


def task_summary(
    t: Task, names: Mapping[UUID, str] | None = None, endings: Mapping[UUID, str] | None = None
) -> TaskSummaryOut:
    """`names` is `{actor_id: display name}` for the page (the list's one batched lookup), `endings`
    `{task_id: ending}` (its other one). A NULL turn author reads as the creator - the promise the
    migration made to older rows."""
    out = TaskSummaryOut.model_validate(t)
    out.ending = (endings or {}).get(t.id)
    out.creator_id = t.creator_actor_id
    out.turn_author_id = t.turn_actor_id or t.creator_actor_id
    if names:
        out.creator_name = names.get(out.creator_id)
        out.turn_author_name = names.get(out.turn_author_id)
    return out


def task_detail(
    t: Task,
    plan: PlanView,
    cost: CostSummaryOut,
    input_urls: dict[str, str] | None = None,
    can_edit: bool = False,
    can_curate: bool = False,
    is_creator: bool = False,
    creator_name: str | None = None,
    machine: MachineOut | None = None,
    can_end_control: bool = False,
    *,
    waiting_for: str | None = None,
) -> TaskDetailOut:
    return TaskDetailOut(
        id=t.id,
        goal=t.goal,
        title=t.title,
        pinned_at=t.pinned_at,
        archived_at=t.archived_at,
        held_at=t.held_at,
        status=t.status,
        mode=t.mode,
        project_id=t.project_id,
        turn=t.turn,
        created_at=t.created_at,
        scale=t.scale,
        target_pages=t.target_pages,
        deliverable=t.deliverable,
        deliverable_format=t.deliverable_format,
        deliverable_source=t.deliverable_source,
        direct_bot=t.direct_bot,
        creator_id=t.creator_actor_id,
        turn_author_id=t.turn_actor_id or t.creator_actor_id,
        creator_name=creator_name,
        plan=plan,
        cost=cost,
        criteria=t.criteria,
        parent_task_id=t.parent_task_id,
        bot_pins=[
            BotPinOut(name=str(p.get("name", "")), version=p.get("version"))
            for p in t.bot_pins
            if p.get("name")
        ],
        origin_routine_id=t.origin_routine_id,
        origin_conversation_id=t.origin_conversation_id,
        origin_message_seq=t.origin_message_seq,
        can_edit=can_edit,
        can_curate=can_curate,
        is_creator=is_creator,
        machine=machine,
        plan_epoch=t.plan_epoch,
        can_end_control=can_end_control,
        waiting_for=waiting_for,
        inputs=[
            TaskInputOut(
                filename=str(i["filename"]),
                bytes=int(i["bytes"]),
                media_type=str(i.get("media_type") or "text/plain"),
                url=(input_urls or {}).get(str(i["filename"]), ""),
            )
            for i in t.inputs
        ],
    )


def task_accepted(t: Task) -> TaskAcceptedOut:
    return TaskAcceptedOut(
        task_id=t.id,
        stream_url=links.task_stream(t.id),
        events_url=links.task_events(t.id),
        turn=t.turn,
        last_seq=t.event_seq,
    )


class MoveIn(BaseModel):
    # null moves the task OUT of any project (--personal). A field that is present-and-null differs
    # from absent, so the client always sends it explicitly.
    project_id: UUID | None = None


class SteerIn(BaseModel):
    directive: str = Field(
        min_length=1, max_length=DIRECTIVE_MAX, description="what to change, in plain words"
    )
    command_id: UUID | None = Field(
        default=None, description="the client's id for this command; a resend is idempotent"
    )
    issued_epoch: int | None = Field(
        default=None, description="the plan epoch the screen showed when typing began"
    )


class ReplanIn(BaseModel):
    directive: str = Field(min_length=1, max_length=DIRECTIVE_MAX, description="why the plan needs rewriting")


# The small action acknowledgements. Each write-that-steers returns the one or two facts it changed,
# typed - never a bare dict across the boundary. One shape per distinct payload; the field
# names are exactly what the CLI and web already read, so these are a type over today's JSON.
class StatusOut(BaseModel):
    """A steering action whose whole result is the task's new machine status (cancel/pause/resume)."""

    status: str


class MoveOut(BaseModel):
    status: str
    project_id: str | None  # null once the task is back in personal project


class TaskBudgetIn(BaseModel):
    """The task's new USD ceiling (B3): dollars and cents, more than zero."""

    usd: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class TaskBudgetOut(BaseModel):
    usd: Decimal
    previous_usd: Decimal | None
    gate_id: UUID | None  # the open budget gate to resolve to resume; null when none is open


class ReplanOut(BaseModel):
    replanned: str  # the directive that drove the replan
    steps: int  # how many plan nodes the new plan has


class SteerOut(BaseModel):
    """A steer as its author's screen tracks it: the command, the epoch it opened, its state."""

    steered: str  # the directive
    command_id: UUID
    epoch: int
    state: str  # accepted | applied | rejected | retracted


def steer_out(row: Steer) -> SteerOut:
    return SteerOut(steered=row.text, command_id=row.command_id, epoch=row.epoch, state=row.state)


class DeletedOut(BaseModel):
    deleted: str  # the id of the task that is gone


class FeedbackOut(BaseModel):
    rating: str  # "up" | "down" - the turn's current mood
    turn: int  # which conversation turn it graded
