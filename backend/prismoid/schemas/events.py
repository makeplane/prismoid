# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The event union - the flight recorder, the wire format, and the frontend's contract.

**Two lanes, one socket**:

* **Lane 1 - the record.** Durable `TaskEvent`s with a per-task `seq`. Persisted, replayable,
  and the only frames that set SSE's `id:` - so `Last-Event-ID` is exactly `?since=<seq>` and
  the browser implements our reconnect contract for free.
* **Lane 2 - the wire.** Ephemeral deltas. Never persisted per-frame, never given an `id:`, and
  anchored to the record with `after_seq` so a client can order them against it. **Every Lane-2
  stream terminates in a Lane-1 durable final or snapshot** - which is what makes a dropped
  delta harmless and reconnect boring.

`type` is the discriminator *and* the SSE `event:` name - one string, one meaning, everywhere.

**A note for the implementer of the hub.** Postgres's `NOTIFY` payload caps at 8000 bytes, and a
`task.planned` carrying a twelve-step plan can exceed that. The fan-out therefore notifies with the
event JSON when it fits and with a `{task_id, seq}` pointer when it does not; the hub reads the row
back for pointers. Lane 2 is bounded at 4 KB per frame by the coalescer, so it always rides inline.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeGuard
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter

from prismoid.schemas.context import ContextLaneView, shared_view
from prismoid.schemas.conversation import DocumentFormat
from prismoid.schemas.gate_view import viewer_reason
from prismoid.schemas.ids import ActorId, ArtifactId, GateId, MemoryId, ProjectId, StepId, TaskId
from prismoid.schemas.lead import LeadEndingKind
from prismoid.schemas.machine_need import MachineNeed
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import ArtifactType, BlockedBy, OutcomeStatus, VerifierSuite
from prismoid.schemas.plan import (
    BudgetFigures,
    GateAction,
    GateKind,
    PlanEdit,
    PlanView,
    QuestionSet,
    StepState,
    TaskEnding,
    TaskMode,
    TaskStatus,
)
from prismoid.schemas.principal import CredKind
from prismoid.schemas.qa import QAFinding as QAFindingView
from prismoid.schemas.steering import SafePoint
from prismoid.schemas.usage import Unit
from prismoid.schemas.verification import Diagnosis, Verdict


class ActorRef(BaseModel):
    """The audit envelope - the ONLY place the word "actor" appears in an external payload.

    Everywhere else the API says *user*, *agent*, or *API key*. Internally everything joins on
    one `actor_id`; the split is presentation and lifecycle, not attribution.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["user", "agent", "api_key"]
    id: ActorId
    name: str


class _Envelope(BaseModel):
    """Rule 10: every task-scoped event carries task_id + step_id (+ project_id)."""

    model_config = ConfigDict(frozen=True)

    task_id: TaskId
    step_id: StepId | None = None
    project_id: ProjectId | None = None
    at: dt.datetime
    actor: ActorRef | None = None


class _Durable(_Envelope):
    """Lane 1. `seq` is assigned by `TaskEventRow.append` and is the SSE `id:`."""

    lane: Literal["record"] = "record"
    seq: int


class _Live(_Envelope):
    """Lane 2. No `seq`, therefore no `id:` - a delta must never advance `Last-Event-ID`."""

    lane: Literal["live"] = "live"
    after_seq: int  # the last durable seq, so the client can order this against the record
    channel: str  # step_id + channel - one delta stream per producing thing


# ── Lane 1: the record ────────────────────────────────────────────────────────────────────────


class TaskCreated(_Durable):
    type: Literal["task.created"] = "task.created"
    goal: str
    mode: TaskMode
    parent_task_id: TaskId | None = None
    # A fork's branch point (additive): the parent's `event_seq` when it was forked.
    # The fork reads its parent's thread up to here and never the turns after - those are the
    # other branch. None on a task with no parent, and on every fork made before the field.
    parent_seq: int | None = None
    # Intent attach provenance (additive): "slug:keyword" per connector the door
    # attached on its own - the wear-the-reason law, so an auto-mounted credential is legible
    # on the record. Empty for explicit attaches and everything before this field existed.
    connectors_auto: list[str] = []
    credential: CredKind | None = None  # the asker's credential kind; None off the task doors and before


class TaskClarifying(_Durable):
    """At most once, before planning."""

    type: Literal["task.clarifying"] = "task.clarifying"
    gate_id: GateId


class TaskPlanned(_Durable):
    """The routed record rides here (zero new wire kinds): each turn's lane, the clause that chose it,
    the reason, its Machine need (None before the field), and a failed planner's fallback."""

    type: Literal["task.planned"] = "task.planned"
    plan: PlanView
    # `route` (envelope `lane` is record|live): express|standard|fleet|longform|… (old `sectioned` = longform)
    route: Annotated[str, BeforeValidator(lambda r: "longform" if r == "sectioned" else r)] = "standard"
    route_source: str = Field(default="planner", description="dial|detector|anchor|hatch|planner|lead")
    route_why: str = ""  # ≤200 - wear the reason
    machine: MachineNeed | None = None
    machine_why: str = ""  # ≤200
    fallback: str = ""  # ≤300: the planner failed and the one-step floor ran - why. Empty otherwise


class TaskStatusChanged(_Durable):
    """paused / resumed / running / waiting / failed - one event, the status is the payload."""

    type: Literal["task.status"] = "task.status"
    status: TaskStatus
    reason: str = ""
    ending: TaskEnding | None = None  # `could_not` on a failure an agent answered; None otherwise
    summary: str = ""  # on `cancelled`: the code-built account of what the turn finished and never did


class TaskDequeued(_Durable):
    """The worker took the turn's workflow off the queue: created/continued → dequeued is queue hop 1."""

    type: Literal["task.dequeued"] = "task.dequeued"
    turn: int = 1


class StepDequeued(_Durable):
    """The step's workflow started on the step queue: running → step.dequeued is queue hop 2, per step."""

    type: Literal["step.dequeued"] = "step.dequeued"


class TaskMoved(_Durable):
    type: Literal["task.moved"] = "task.moved"
    from_project_id: ProjectId | None = None
    to_project_id: ProjectId | None = None


class TaskBudgetSet(_Durable):
    """Someone set the task's USD ceiling (B3) - the answer a "Budget reached" gate asks for. The
    gate is resolved separately: its accept resumes the parked wave, and the next boundary checks
    the budget again."""

    type: Literal["task.budget_set"] = "task.budget_set"
    usd: Decimal
    previous_usd: Decimal | None = None


class MachineCreated(_Durable):
    """The task's Machine was created: the early boot, the first step that needed a frame, or a
    fork's seed. `seeded_as_of` is when the parent's files it started from were taken (a fork only)."""

    type: Literal["machine.created"] = "machine.created"
    profile: str
    seeded_as_of: dt.datetime | None = None


class MachineSlept(_Durable):
    """The Machine's checkpoint was stored and its frame destroyed. `after_s` is how long it was
    idle or parked; `dropped_bytes` is set when `/work` was over the size cap and was not kept.
    `actor` (the envelope) is set only for Sleep now."""

    type: Literal["machine.slept"] = "machine.slept"
    reason: str  # SleepReason
    after_s: int | None = None
    dropped_bytes: int | None = None


class MachineWoke(_Durable):
    """A new frame restored the Machine for a step. `actor` is the turn's author."""

    type: Literal["machine.woke"] = "machine.woke"
    turn: int
    profile: str
    browser_restored: bool = True
    from_park: bool = False


class MachineRebuilt(_Durable):
    """The Machine started fresh: its checkpoint expired or was lost (a rebuild, never a failure)."""

    type: Literal["machine.rebuilt"] = "machine.rebuilt"
    reason: str  # RebuildReason
    retention_days: int | None = None


class MachineReset(_Durable):
    """An editor reset the Machine (`actor`): its frame and checkpoint are gone."""

    type: Literal["machine.reset"] = "machine.reset"


class MachineControlClaimed(_Durable):
    """Someone took control of the Machine's browser (ADR-065). The envelope's `actor`
    is the holder. `private`: the mask is on - nobody else sees the screen until it ends. A holder
    who switches privacy on or off is recorded again; a reload's re-claim is not."""

    type: Literal["machine.control_claimed"] = "machine.control_claimed"
    private: bool = False


class MachineControlEnded(_Durable):
    """A control lease ended. The envelope's `actor` is the person who held it;
    `ended_by` is the Project owner who ended someone else's (`reason == "ended"`), else None.
    `held_s` is how long it was held. `lease_since` names the lease that ended (its claim's
    `since`, the lease's identity - a person's name is not one); absent on endings recorded before
    it was carried. Only claims and endings are recorded - never input."""

    type: Literal["machine.control_ended"] = "machine.control_ended"
    reason: str  # ControlEnd: released | expired | grace | cap | ended | demoted | disabled | lost
    held_s: int = 0
    ended_by: ActorRef | None = None
    lease_since: dt.datetime | None = None


class TaskRenamed(_Durable):
    """A label change, not a state transition - the record keeps every name the work wore."""

    type: Literal["task.renamed"] = "task.renamed"
    title: str


class TaskCancelled(_Durable):
    type: Literal["task.cancelled"] = "task.cancelled"
    reason: str = ""


class TaskDelivered(_Durable):
    type: Literal["task.delivered"] = "task.delivered"
    summary: str  # written for a human who wasn't watching
    artifact_ids: list[ArtifactId] = Field(default_factory=list[ArtifactId])
    ending: TaskEnding | None = None  # done | partial | could_not (a declined answer); None on older frames


class TaskContinued(_Durable):
    """A settled task accepted a new user message and re-entered planning - same row, same
    record, same stream, next turn. The follow-up IS the task continuing; it is
    never a new task. This frame doubles as the status signal (the row is `planning` the
    moment it lands, the way `task.delivered` implies `delivered`)."""

    type: Literal["task.continued"] = "task.continued"
    message: str
    turn: int
    client_token: str | None = None  # idempotency echo: continue_task compares this on replay
    # THIS turn's explicit dials, None = untouched: the row alone cannot tell a person's fresh choice from
    # the resolver's own past stamp. Frames appended before a field replay without it: untouched.
    scale: str | None = None
    target_pages: int | None = None
    deliverable: str | None = None
    deliverable_format: str | None = None
    attached: list[str] | None = None  # the filenames THIS turn attached; None before the field
    credential: CredKind | None = None  # the sender's credential kind; None off the task doors and before
    # A follow-up that changed the cap says so - who is the envelope's actor.
    budget_before: dict[str, Any] | None = None
    budget_after: dict[str, Any] | None = None
    epoch: int | None = None  # the epoch this turn opened at


class TaskProposed(_Durable):
    """The Lead proposed WORK (the card under its reply): Start is a follow-up with the deliverable dials
    set, Not now is nothing - consent is a tap, never the model's. Nothing executes on this frame."""

    type: Literal["task.proposed"] = "task.proposed"
    goal: str
    deliverable: str | None = None  # a kind word: document | spreadsheet | deck | code | collection
    deliverable_format: DocumentFormat | None = None  # a document's file format: Start sets this dial too
    why: str = ""
    turn: int = 1


class TaskFeedback(_Durable):
    """The user graded a turn: thumbs up or down on the answer they were given.
    Append-only like every judgment in the record - a changed mind is a second frame, and the
    newest one is the turn's current mood. Never machine state; purely the human's verdict,
    kept where provenance lives (the actor rides the envelope)."""

    type: Literal["task.feedback"] = "task.feedback"
    rating: Literal["up", "down"]
    turn: int = 1  # which turn was graded - the buttons reset when the next turn opens


class PlanEdited(_Durable):
    """A replan is an edit with history, never a new plan."""

    type: Literal["plan.edited"] = "plan.edited"
    edit: PlanEdit


class SteerApplied(_Durable):
    """Running work took a steer (`step_id`: the step, none for a re-plan); what it superseded, could not undo."""

    type: Literal["steer.applied"] = "steer.applied"
    command_id: UUID
    step_title: str = ""
    author: str = ""  # the steer's author as a safe display name; filled by steering.apply
    safe_point: SafePoint
    superseded: list[ArtifactId] = Field(default_factory=list[ArtifactId])
    already_done: list[str] = Field(default_factory=list[str])  # calls that acted before it: unchangeable


class SteerRejected(_Durable):
    """The system refused a steer it had accepted, by rule; the reason is shown to everyone."""

    type: Literal["steer.rejected"] = "steer.rejected"
    command_id: UUID
    reason: str


class SteerRetracted(_Durable):
    """Its author withdrew a steer before it applied (`actor` is the author)."""

    type: Literal["steer.retracted"] = "steer.retracted"
    command_id: UUID


class PauseApplied(_Durable):
    """A pause took hold: a step parked at a tool dispatch (`step_id`), or the frontier held."""

    type: Literal["pause.applied"] = "pause.applied"
    safe_point: SafePoint


class StepStateChanged(_Durable):
    type: Literal["step.state"] = "step.state"
    state: StepState
    title: str = ""
    agent: str | None = None
    attempt: int = 1
    detail: str = ""
    model_ref: str | None = None  # a fleet seat's catalog pin, as data (no title parsing); None elsewhere
    # A guarded step's typed ending: `partial` on a DONE frame, `blocked`
    # (with `blocked_by`) on an ESCALATED one or on the RUNNING frame of a sign-in hand-off. None
    # everywhere else; readers tolerate its absence (deploys are not atomic).
    outcome: OutcomeStatus | None = None
    blocked_by: BlockedBy | None = None
    ending: LeadEndingKind | None = None  # the Lead step's terminal frame: how its run ended
    findings: bool | None = None  # an unattended run's delivery: did it find news for its owner?


class StepNarration(_Durable):
    """The durable terminal of a Lane-2 `message.*` stream: the full markdown, once."""

    type: Literal["step.narration"] = "step.narration"
    markdown: str


class StepPrompt(_Durable):
    """The assembled prompt a step's agent was actually given - the one context injection the
    record did not carry (the DeepSeek-Harness lesson worth stealing).

    For a bot author, "why did my bot do that" is unanswerable from narrations alone: the answer
    is in the prompt - the goal as retold, the answers folded in, the skills named, the
    acceptance lines, the retry gotchas. One durable frame per attempt, clipped at the cap with
    an honest marker, never silently. The mounted FILES (thread.md, context.md, sources) are
    named by the prompt rather than duplicated into it - they are already durable elsewhere.
    """

    type: Literal["step.prompt"] = "step.prompt"
    attempt: int
    agent: str  # the role that received it - a bot's name or a built-in's
    prompt: str  # the text handed to `runner.run`, clipped at PROMPT_SNAPSHOT_MAX_CHARS
    truncated: bool = False
    # The one catalog row this attempt ran on, when it ran on one: a fleet seat, or a class
    # pinned per attempt (computer use), whose retry rotates rows - two receipts naming
    # different rows are that failover, on the record. None for a fallback chain.
    model_ref: str | None = None


# One frame's worth, not one context window's: the snapshot exists to be READ in a task view,
# and the pieces past this size (mounted files) are durable in their own right and merely named.
PROMPT_SNAPSHOT_MAX_CHARS = 32_000


class ToolCalled(_Durable):
    """The journal. Every `exec` in a frame lands here - including the ones that were refused,
    which is how an egress denial is *visible* rather than mysterious."""

    type: Literal["tool.called"] = "tool.called"
    tool: str
    ok: bool
    summary: str = ""  # never the raw args: they can carry secrets
    duration_ms: int = 0
    denied_reason: str = ""
    # The live↔durable reconcile key (the progress tree): the same id the ephemeral `activity`
    # frames carried, so the web lands both lanes on ONE row. Additive - old rows deserialize to "".
    tool_call_id: str = ""
    # The step attempt this call ran in (the engine stamps `step.attempt`). The recording
    # verifier counts ONE attempt's acting calls against that attempt's recording - a retried
    # step's earlier attempts journaled calls too. Additive - old rows deserialize to None.
    attempt: int | None = None
    ref: str | None = None  # the sha of the page a `web.fetch` kept (`operations/sources.py`), off the label


class GateOpened(_Durable):
    type: Literal["gate.opened"] = "gate.opened"
    gate_id: GateId
    kind: GateKind
    title: str
    questions: QuestionSet | None = None  # present iff kind == input
    # The gate's evidence (additive): an approve gate's per-call blocks (label line + capped args preview),
    # a budget gate's ceiling sentence, a connect gate's "why". Empty on input gates. Old rows: the default.
    reason: str = ""
    # CONNECT gates only: which service the in-conversation block offers.
    connector_slug: str = ""
    # APPROVE gates only: where the ask came from ("connector" | "browser"), the host a browser
    # ask is about, the scopes the human may choose (empty for a connector gate or a browser
    # danger ask), and why the rules asked, said first. Old rows replay with the defaults.
    source: str = "connector"
    host: str = ""
    scopes: list[str] = Field(default_factory=list)
    asks_because: list[str] = Field(default_factory=list)
    # BUDGET gates (B3): "task" | "project" | "" - which ceiling, so the dock picks its face.
    budget_scope: str = ""
    # BUDGET gates: the Bursar's figures, so the dock's raise clears the wave. Old rows replay None.
    budget_figures: BudgetFigures | None = None
    expires_at: dt.datetime | None = None


class GateResolved(_Durable):
    type: Literal["gate.resolved"] = "gate.resolved"
    gate_id: GateId
    action: GateAction
    content: dict[str, str | int | float | bool | list[str]] | None = None
    defaulted: list[str] | None = None  # ids in `content` skipped to defaults; None: unrecorded


class UsageRecorded(_Durable):
    """Every provider call, on the stream and on the ledger. No metering, no merge."""

    type: Literal["usage.recorded"] = "usage.recorded"
    provider: str
    model: str
    family: Family
    units: dict[Unit, int]
    cost_usd: Decimal


class ArtifactProduced(_Durable):
    type: Literal["artifact.produced"] = "artifact.produced"
    artifact_id: ArtifactId
    artifact_type: ArtifactType
    title: str
    # The published file's basename. The naming law (kit/artifact-icon) speaks filename-aware -
    # a `document` is a Page (.md/.txt) or a Document (.docx/.pdf) - and the transcript card and
    # task rail render from THIS frame, not from an API row. Optional: rows appended before the
    # field existed replay without it, and consumers fall back to the kind's parent word.
    filename: str | None = None
    sha256: str
    bytes: int
    # deliverable | working. Working rows (outline, sections, figures) still land on
    # the record - the progress tree shows sections arriving - but the shelf and the verify
    # queue filter on this. Defaulted so pre-lane rows replay as what they were: deliverables.
    role: str = "deliverable"


class ArtifactRevised(_Durable):
    """A new immutable revision on an existing lineage - a human edit saved, or a
    follow-up's re-publish of the same deliverable. Append-only like everything else: the old
    version keeps its bytes, its verification, and its history; this frame is how every surface
    learns which id is now the lineage's face."""

    type: Literal["artifact.revised"] = "artifact.revised"
    artifact_id: ArtifactId  # the NEW row
    supersedes_id: ArtifactId  # the row it replaces as latest
    lineage_id: ArtifactId
    revision: int
    artifact_type: ArtifactType
    title: str
    filename: str | None = None  # basename, for the naming law - same contract as ArtifactProduced
    sha256: str
    bytes: int
    role: str = "deliverable"  # same contract as ArtifactProduced


class ArtifactSnapshot(_Durable):
    """A durable checkpoint of a long text write (~every 8 KB), so a reconnect mid-write renders
    the latest state instead of nothing. This is what makes Lane-2 loss survivable."""

    type: Literal["artifact.snapshot"] = "artifact.snapshot"
    path: str
    offset: int
    content: str


class VerificationSigned(_Durable):
    type: Literal["verification.signed"] = "verification.signed"
    artifact_id: ArtifactId
    verdict: Verdict
    suite: VerifierSuite
    verifier_family: Family | None = None
    signature: str


class QAReviewed(_Durable):
    """The long-form lane's pre-delivery editorial verdict - NOT verification.

    One frame per QA pass on the assembly step: pass, revise (with the findings the rebuild was handed), or
    pass_unread (the read could not run; checks were clean; the note shipped). The findings survive here for
    the follow-up turn - "fix section 7" reads this frame, not a scratch file. Clamped at the policy layer
    before emit; the reviewer identity comes from the LEDGER, never from the model's self-report.
    """

    type: Literal["qa.reviewed"] = "qa.reviewed"
    verdict: str  # QAOutcomeKind: pass | revise | pass_unread
    attempt: int = 1
    findings: list[QAFindingView] = Field(default_factory=list[QAFindingView])
    summary: str = ""
    reviewer_model: str = ""
    reviewer_family: Family | None = None


class EscalationRaised(_Durable):
    """We stopped, and we say why - in a form a human can act on."""

    type: Literal["escalation.raised"] = "escalation.raised"
    diagnosis: Diagnosis


class ContextAssembled(_Durable):
    """What this task was told beyond its ask - ids, lanes, token counts, truncations.

    The context ledger's spend record: every memory a turn was told is named here - its pack at
    planning, then each note a `memory_search` returned. "Why did the machine know that" is this event.
    Payload stays bounded (previews, not full texts) so it rides NOTIFY inline.
    """

    type: Literal["context.assembled"] = "context.assembled"
    lanes: list[ContextLaneView] = Field(default_factory=list[ContextLaneView])
    total_tokens: int = 0
    memory_ids: list[MemoryId] = Field(default_factory=list[MemoryId])


class MemoryStub(BaseModel):
    """One minted memory, sized for the transcript marker (the ledger holds the full row)."""

    model_config = ConfigDict(frozen=True)

    id: MemoryId
    kind: str
    text: str  # ≤160 chars - clamped at emit


class MemoryWritten(_Durable):
    """memory_extract ran. `count: 0` is valid and load-bearing - "extraction ran, kept
    silence" - and its existence is the replay guard that makes remember() idempotent
    **per turn**: the guard compares `turn` against the task's, so each turn's delivery
    gets its own extraction (old events deserialize as turn 1)."""

    type: Literal["memory.written"] = "memory.written"
    count: int = 0
    items: list[MemoryStub] = Field(default_factory=list[MemoryStub])
    turn: int = 1
    skipped: str | None = None  # "project" | "shared" - the project fence stood extraction down


class BotMemoryWritten(_Durable):
    """memory_extract ran for a DM turn's bot: what the task's keeper bot learned. Its own
    type - not a field on `memory.written` - so `latest_of_type` keeps each extraction's
    per-turn idempotence trivial, and the rail can say "{bot} learned" in its own voice.
    `count: 0` is the same honest silence `memory.written` records."""

    type: Literal["bot_memory.written"] = "bot_memory.written"
    bot: str = ""
    count: int = 0
    items: list[MemoryStub] = Field(default_factory=list[MemoryStub])
    turn: int = 1


# ── Lane 2: the wire ──────────────────────────────────────────────────────────────────────────


class MessageStart(_Live):
    type: Literal["message.start"] = "message.start"
    agent: str = ""


class MessageDelta(_Live):
    """Markdown, append-only. Never HTML (XSS), never a retro-edit of what was already sent -
    a correction is a new message, not a rewrite of streamed history."""

    type: Literal["message.delta"] = "message.delta"
    text: str


class MessageEnd(_Live):
    """Terminates in a durable `step.narration` carrying the same markdown in full."""

    type: Literal["message.end"] = "message.end"


class ArtifactDelta(_Live):
    """Text-like artifacts stream their content. Binary artifacts NEVER stream bytes - an xlsx
    streams `progress` facts (sheets written, rows) and renders on harvest."""

    type: Literal["artifact.delta"] = "artifact.delta"
    path: str
    text: str
    # Character offset of `text` within the document. 0 + whole-current is the write_file
    # preview's shape; the in-composition stream (the tap's draft pour) sends true
    # increments so the web can assemble the growing draft.
    offset: int = 0


class Progress(_Live):
    """The no-dead-air signal: an honest sign of life at least every 10 s while a step runs.
    Frontend-only - never fed back to the model."""

    type: Literal["progress"] = "progress"
    phase: str  # "thinking", "writing", "verifying", "installing"…
    elapsed_s: float
    tokens_so_far: int = 0
    detail: str = ""


class Activity(_Live):
    """One tiny operation, as it happens - the progress tree's live rows (the plan's
    micro-activity taxonomy). Kinds narrate the machine's layers:

      tool     - a tool call: `start` the moment pydantic-ai validates it, `end` with
                 ok + duration when the result lands. The durable `tool.called` remains
                 the record; the web reconciles the two by `tool_call_id`.
      model    - a model request round: `start` when the request opens ("Thinking…"),
                 `end` when the first visible part arrives ("Thought for Ns").
      thinking - the model's reasoning STREAMING (`delta` + `text`), when a model emits
                 thinking parts. Ephemeral like every Lane-2 frame: never persisted,
                 absent on replay - the live-only richness, like message deltas.
      engine   - the step machinery around the model: frame prep, input mounting,
                 skill loading, harvest, the system-side publishes. Taking the task's
                 Machine carries `tool` "machine": its `end` is the step holding it.
      phase    - the pre-step machinery: clarifier / planner / namer brackets, emitted
                 by the OPERATION around the (emit-less) agents; on these frames `tool`
                 carries WHO is working - the machinery agent's name - for the tree's
                 identity rows.

    `label` on TOOL frames follows tool.called.summary's redaction posture: locators only - a path,
    a clamped command, `type:path`, a count - never file content, never raw args (`ai/tools/labels.py`
    is the one policy). Engine/phase labels are the machinery's own sentences, clamped to the same width."""

    type: Literal["activity"] = "activity"
    kind: Literal["tool", "model", "thinking", "engine", "phase"]
    phase: Literal["start", "end", "delta"]
    tool: str = ""  # tool frames: journal vocabulary (raw name if unknown); phase frames: WHO
    label: str = ""  # ≤160 chars (labels.clamp at every emitter); see the class docstring
    text: str = ""  # thinking deltas only, ≤1200-char slices like message.delta
    tool_call_id: str = ""
    ok: bool | None = None  # end frames only
    duration_ms: int = 0  # end frames only
    wait_s: int | None = None  # Machine `start` frames: the step's wait bound (`settings.machines.wait_s`)


# ── Lane 2: presence ──────────────────────────────────────────────────────────────────────────


class _Presence(_Envelope):
    """Who is on this task, right now. State, not content - never durable, never
    replayed, no `id:` on the wire. `lane = "live"` so the hub's live adapter routes it."""

    lane: Literal["live"] = "live"
    conn: str  # one open stream (a browser tab); the client passes it, the server sanitizes it
    # Required here - presence is always somebody. Narrowing the envelope's optional `actor` is
    # sound on a frozen model; pyright reads fields as mutable dataclass slots.
    actor: ActorRef  # pyright: ignore[reportGeneralTypeIssues, reportIncompatibleVariableOverride]
    editor: bool = True  # false = shown as watching (a viewer)


class PresenceJoin(_Presence):
    type: Literal["presence.join"] = "presence.join"


class PresenceLeave(_Presence):
    type: Literal["presence.leave"] = "presence.leave"


class PresenceHere(_Presence):
    type: Literal["presence.here"] = "presence.here"


class PresenceTyping(_Presence):
    type: Literal["presence.typing"] = "presence.typing"
    on: bool
    mode: Literal["follow_up", "steer", "answer"]


# ── Lane 2: the screen (ADR-065) ───────────────────────────────────────────────────────────────


class _Screen(_Envelope):
    """What the task's screen knows about its Machine's browser, right now. State, not content:
    never durable, never replayed, no `id:` on the wire, and exempt from "every delta stream ends
    in a durable final", as presence is - the durable record is `machine.control_*`. It
    names the task, never a step. `lane = "live"` so the hub's live adapter routes it."""

    lane: Literal["live"] = "live"


class ScreenState(_Screen):
    """Who drives the browser (`holder`; None = nobody: the agent), whether privately (the mask is
    on for everyone else), and until when unless renewed. Sent on every lease change - a claim, a
    re-claim, Renew, a release, an end, an expiry - and when a closed socket's grace starts. A
    renewal by input sends nothing. The relay re-reads the row when it hears one."""

    type: Literal["screen.state"] = "screen.state"
    holder: ActorRef | None = None
    private: bool = False
    until: dt.datetime | None = None


class ScreenBox(BaseModel):
    """An element's box in viewport CSS px - the screencast's own coordinates."""

    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    w: float
    h: float


class ScreenAction(_Screen):
    """An agent's acting verb that succeeded: the pane flashes `box` for 1.5 s, so
    everyone watching sees where the agent clicked or typed. Host and path only - never a query
    string (the `fetch_label` law). State, not content: never persisted, no `id:`, exempt from the
    durable-snapshot rule like presence; the act's record is its own `tool.called` row."""

    type: Literal["screen.action"] = "screen.action"
    verb: str
    box: ScreenBox | None = None
    host: str = ""
    path: str = ""


# ── the unions ────────────────────────────────────────────────────────────────────────────────

type DurableEvent = Annotated[
    TaskCreated
    | TaskClarifying
    | TaskPlanned
    | TaskStatusChanged
    | TaskDequeued
    | StepDequeued
    | TaskMoved
    | TaskBudgetSet
    | MachineCreated
    | MachineSlept
    | MachineWoke
    | MachineRebuilt
    | MachineReset
    | MachineControlClaimed
    | MachineControlEnded
    | TaskRenamed
    | TaskCancelled
    | TaskDelivered
    | TaskContinued
    | TaskProposed
    | TaskFeedback
    | PlanEdited
    | SteerApplied
    | SteerRejected
    | SteerRetracted
    | PauseApplied
    | StepStateChanged
    | StepNarration
    | StepPrompt
    | ToolCalled
    | GateOpened
    | GateResolved
    | UsageRecorded
    | ArtifactProduced
    | ArtifactRevised
    | ArtifactSnapshot
    | VerificationSigned
    | QAReviewed
    | EscalationRaised
    | ContextAssembled
    | MemoryWritten
    | BotMemoryWritten,
    Field(discriminator="type"),
]

type LiveEvent = Annotated[
    MessageStart
    | MessageDelta
    | MessageEnd
    | ArtifactDelta
    | Progress
    | Activity
    | PresenceJoin
    | PresenceLeave
    | PresenceHere
    | PresenceTyping
    | ScreenState
    | ScreenAction,
    Field(discriminator="type"),
]

# The full wire union, flat: a discriminated union of discriminated unions is not one, and the
# JSON Schema we hand the frontend has to be a single `oneOf` on `type`.
type TaskEvent = Annotated[
    TaskCreated
    | TaskClarifying
    | TaskPlanned
    | TaskStatusChanged
    | TaskDequeued
    | StepDequeued
    | TaskMoved
    | TaskBudgetSet
    | MachineCreated
    | MachineSlept
    | MachineWoke
    | MachineRebuilt
    | MachineReset
    | MachineControlClaimed
    | MachineControlEnded
    | TaskRenamed
    | TaskCancelled
    | TaskDelivered
    | TaskContinued
    | TaskProposed
    | TaskFeedback
    | PlanEdited
    | SteerApplied
    | SteerRejected
    | SteerRetracted
    | PauseApplied
    | StepStateChanged
    | StepNarration
    | StepPrompt
    | ToolCalled
    | GateOpened
    | GateResolved
    | UsageRecorded
    | ArtifactProduced
    | ArtifactRevised
    | ArtifactSnapshot
    | VerificationSigned
    | QAReviewed
    | EscalationRaised
    | ContextAssembled
    | MemoryWritten
    | BotMemoryWritten
    | MessageStart
    | MessageDelta
    | MessageEnd
    | ArtifactDelta
    | Progress
    | Activity
    | PresenceJoin
    | PresenceLeave
    | PresenceHere
    | PresenceTyping
    | ScreenState
    | ScreenAction,
    Field(discriminator="type"),
]

DurableEventAdapter: TypeAdapter[DurableEvent] = TypeAdapter(DurableEvent)
LiveEventAdapter: TypeAdapter[LiveEvent] = TypeAdapter(LiveEvent)
TaskEventAdapter: TypeAdapter[TaskEvent] = TypeAdapter(TaskEvent)

DURABLE_TYPES: tuple[type[_Durable], ...] = (
    TaskCreated,
    TaskClarifying,
    TaskPlanned,
    TaskStatusChanged,
    TaskMoved,
    TaskBudgetSet,
    MachineCreated,
    MachineSlept,
    MachineWoke,
    MachineRebuilt,
    MachineReset,
    MachineControlClaimed,
    MachineControlEnded,
    TaskRenamed,
    TaskCancelled,
    TaskDelivered,
    TaskContinued,
    TaskProposed,
    TaskFeedback,
    PlanEdited,
    SteerApplied,
    SteerRejected,
    SteerRetracted,
    PauseApplied,
    StepStateChanged,
    StepNarration,
    StepPrompt,
    ToolCalled,
    GateOpened,
    GateResolved,
    UsageRecorded,
    ArtifactProduced,
    ArtifactRevised,
    ArtifactSnapshot,
    VerificationSigned,
    QAReviewed,
    EscalationRaised,
    ContextAssembled,
    MemoryWritten,
    BotMemoryWritten,
)

LIVE_TYPES: tuple[type[_Live], ...] = (
    MessageStart,
    MessageDelta,
    MessageEnd,
    ArtifactDelta,
    Progress,
    Activity,
)

# Lane 2 too, but their own family: presence is state about the room, not a step's delta.
PRESENCE_TYPES: tuple[type[_Presence], ...] = (PresenceJoin, PresenceLeave, PresenceHere, PresenceTyping)

# Lane 2 too: state about the task's browser.
SCREEN_TYPES: tuple[type[_Screen], ...] = (ScreenState, ScreenAction)


def _type_name(cls: type[BaseModel]) -> str:
    return str(cls.model_fields["type"].default)


def event_type_names() -> list[str]:
    """Every `type` in the union - the SSE `event:` names, and the frontend's switch statement."""
    return [_type_name(c) for c in (*DURABLE_TYPES, *LIVE_TYPES, *PRESENCE_TYPES, *SCREEN_TYPES)]


def is_durable(event: BaseModel) -> TypeGuard[DurableEvent]:
    """Only durable frames set SSE's `id:`. This predicate is that rule, in one place.

    **A `TypeGuard`, so the rule is enforced by the type checker and not by memory.** Reading
    `event.seq` off a Lane-2 frame is now a *compile* error rather than a runtime one - and the
    runtime one would have been the worst kind: an `id:` on an ephemeral delta, a browser resuming
    from a seq no durable event ever had, and durable events silently skipped on every reconnect.
    """
    return isinstance(event, _Durable)


# ── redaction on read ─────────────────────────────────────────────────────────────────────────

_HIDDEN_PROMPT = "[hidden: a step prompt may carry the task creator's private memory]"


def redact_for[E: BaseModel](event: E, *, viewer_id: UUID, creator_id: UUID) -> E:
    """Private-bearing fields are the creator's to read. Pure; `seq` and counts
    stay, so cursors and meters hold; only contents go. Everyone else's view of old records
    (before the fence, or a personal task moved into a Project) is covered here, at the door."""
    if viewer_id == creator_id:
        return event
    if isinstance(event, ContextAssembled):
        if (shared := shared_view(event.lanes, event.memory_ids)) is None:
            return event
        return event.model_copy(update={"lanes": shared[0], "memory_ids": shared[1]})
    if isinstance(event, (MemoryWritten, BotMemoryWritten)) and event.items:
        return event.model_copy(update={"items": []})
    if isinstance(event, StepPrompt):
        return event.model_copy(update={"prompt": _HIDDEN_PROMPT})
    if isinstance(event, GateOpened):  # a device-away command, a connector approval's arguments
        return event.model_copy(update={"reason": viewer_reason(event.reason, event.kind, event.source)})
    return event
