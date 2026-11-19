# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`TaskDeps`: the ports operations fills and the task facts an agent run is handed.

The engine reaches no database: what it records it records through a callable declared here."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue
from pydantic_ai.messages import ModelMessage

from prismoid.ai.frames.driver import FrameDriver
from prismoid.ai.gateway.meter import EmitUsage
from prismoid.ai.tools.browser import BrowserPorts
from prismoid.ai.tools.call_rows import JournalTool
from prismoid.ai.tools.chat import ChatHands
from prismoid.ai.tools.frame_tools import PreviewTool
from prismoid.ai.tools.memory import SearchMemory
from prismoid.ai.tools.proposals import ProposeTask
from prismoid.ai.tools.research import KeepPage
from prismoid.ai.tools.routines import ProposeRoutine
from prismoid.ai.tools.skills import SkillCard
from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.call_log import CallPhase
from prismoid.schemas.connectors import ConnectorSpec
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.conversation import ChatLiveEvent
from prismoid.schemas.egress import Taint
from prismoid.schemas.events import LiveEvent
from prismoid.schemas.ids import ConversationId, ProjectId, StepId, TaskId
from prismoid.schemas.lead import LeadFacts
from prismoid.schemas.outputs import Outcome
from prismoid.schemas.plan import BudgetFigures, LaneRole, PlanFacts
from prismoid.schemas.sizing import WebEffort
from prismoid.schemas.steering import SafePoint, SteerNote, SteerPending

# The sandbox, as the frames layer defines it: one definition, never a hand-copied Protocol.
FrameLike = FrameDriver

# A task step's Lane-2 frames, or a conversation reply's: the tap decides by which id the deps carry.
EmitEvent = Callable[[LiveEvent | ChatLiveEvent], Awaitable[None]]


# The steering mailbox's hands: operations reads and writes the steers on its own short
# sessions; the engine only asks.
class SteerInbox(Protocol):
    """The contract the safe points rely on:

    * `pending(after_epoch)` returns the steers in ascending epoch order (oldest first), so the
      newest one's epoch is what `told` moves to;
    * `applied` may be called repeatedly for the same steers (once per parallel call in a batch,
      then again at the request that tells the run), and must be idempotent: the first safe point
      stands. The operations inbox gets this from a compare-and-set settle;
    * `paused_here` may be called repeatedly for the same park (once per parallel call), and must
      record one fact: the operations inbox dedupes per run.
    """

    told: int  # the newest epoch injected into this run; saved with a park, restored on resume
    park_pause: bool  # operations' switch: may a pause park this run

    async def pending(self, after_epoch: int) -> SteerPending: ...
    async def applied(self, steers: Sequence[SteerNote], point: SafePoint) -> None: ...
    async def paused_here(self, point: SafePoint) -> None: ...


# A step's eyes on its typed ending (`ai/capabilities/output_checks.py`): operations looks (the
# proof check reads the frame and its manifest); the capability decides, and what the run's ending
# stands on comes back on the runner (`AgentRunner.checked`), never here.
class EndingChecks(Protocol):
    """`can_hand_off`: a person can still be handed the browser; `acceptance`: the lines the step is
    judged on."""

    can_hand_off: bool
    acceptance: Sequence[str]

    async def proof(self, outcome: Outcome, messages: Sequence[ModelMessage]) -> str | None: ...


class MachinePort(Protocol):
    """A step on paper's Machine on demand (`capabilities/machine_acquire.py`): `acquire` starts or
    binds it once, carries the paper's files across and answers its frame."""

    async def acquire(self) -> FrameDriver: ...


@dataclass(frozen=True, slots=True)
class RunKey:
    """Which generation of a step's call log a run reads and writes."""

    step: StepId
    generation: int  # 0, 1, …: a fresh ladder attempt or a fence starts the next one
    log_version: int  # durability.LOG_VERSION when the generation opened


@dataclass(frozen=True, slots=True)
class CallEntry:
    """One logged unit's intent or result; the payload is plaintext, the store encrypts it."""

    unit: str  # "<op>@<tool_call_id>[!approved]" | "<op>#<n>" | "segment#<n>"
    phase: CallPhase
    payload: JsonValue
    fingerprint: str | None = None  # model requests only (content hash, 16 hex)


@dataclass(frozen=True, slots=True)
class Generation:
    """A step's latest generation: its key (None while the step has no rows) and its entries."""

    key: RunKey | None
    entries: Mapping[tuple[str, str], CallEntry]
    earlier: Sequence[Mapping[tuple[str, str], CallEntry]] = ()  # every lower generation's, oldest first


class CallLog(Protocol):
    """The per-call store: `record` is durable on return, and a duplicate is a no-op."""

    async def latest(self, step: StepId) -> Generation: ...
    async def record(self, key: RunKey, entry: CallEntry) -> None: ...


@dataclass(slots=True)
class TaskDeps:
    """What `ai.agents.build(name, deps)` closes over. One object, injected by `operations`."""

    # None for a model call outside any task (a skill or bot selection eval): its spend is recorded
    # with a null `task_id`.
    task_id: TaskId | None = None

    step_id: StepId | None = (
        None  # the step this run serves, on its spend and live frames; None outside a step
    )
    project_id: ProjectId | None = None  # the task's Project, on its spend and live frames; None outside one

    frame: FrameLike | None = None  # the run's sandbox; None for a frameless run (the judge, a reply)

    # The browser family's hands: the frame's `browser`/`allow` verbs, live only on
    # a browser-profiled frame - so a step whose agent was not mounted with one gets no toolset
    # at all (`build()`'s `deps.browser is not None` check), the same absent-beats-broken law
    # every worker-process tool lives by.
    browser: BrowserPorts | None = None

    emit_event: EmitEvent | None = None  # Lane-2 deltas
    emit_usage: EmitUsage | None = None  # every provider call emits one
    journal_tool: JournalTool | None = None  # every tool call, ok or denied

    # The Lane-2 tap. An *intent* - a path and its content - which `operations` turns
    # into an ephemeral `artifact.delta` and, every ~8 KB, a durable `artifact.snapshot`. Same shape
    # as `journal_tool`, and for the same reason: the engine cannot reach a database and does not
    # know there is one.
    preview_tool: PreviewTool | None = None
    # The source store: `web.fetch` hands it each page's text and records the sha it answers, so the
    # receipt can check a quote against the page after the task ends. A CLOSURE operations wires
    # for a step run (`operations/sources.py`); None elsewhere - the page is read, not kept.
    keep_page: KeepPage | None = None
    # A task fact: every source id the task's earlier reads were given, to its page, so a citing step
    # cites a page an earlier step read without re-typing its URL. Set by `equip` for a citing agent.
    sources: Mapping[str, str] = field(default_factory=dict[str, str])
    # The routine proposer: `routine.create`'s hands. A CALLBACK, not an import -
    # the engine stays persistence-free (the emit_usage precedent); operations wires a closure
    # that creates the routine BORN PAUSED under the task's owner and knocks the inbox. None ⇒
    # the tool does not exist for this run (absent beats broken).
    propose_routine: ProposeRoutine | None = None
    # The task proposer: `propose_task`'s hands, the same shape one lane over. Wired
    # ONLY by the conversation reply lane; the closure records a proposal card on the bot's
    # message row and never starts anything - consent is the door's, not the model's.
    propose_task: ProposeTask | None = None
    # The card verbs (`continue_card`, `accept_pending`, `decline_pending`): wired ONLY by the
    # conversation reply lane, each verb only while its card exists. None - no card verbs.
    chat: ChatHands | None = None
    # The steering inbox: set ONLY by the step runner (`execution._attempt` and the
    # express attempt). None ⇒ no `Steering` capability - judges, the light agents and verification
    # never take a steer.
    steering: SteerInbox | None = None
    # The task's live spend against its cap: the run guard's eyes, read at
    # every safe point. A CLOSURE operations wires (the `propose_task` precedent) on its own short
    # session; None when the task has no cap. Set ONLY by the step runner - no closure, no brake.
    spend: Callable[[], Awaitable[BudgetFigures | None]] | None = None
    # The typed ending's checks: set ONLY by the step runner, for an agent that finishes with an
    # Outcome; None - the run ends as the model said.
    ending_checks: EndingChecks | None = None
    # The step's Machine on demand: set ONLY by the step runner for a step that starts on paper; the
    # first tool that needs the computer acquires it. None - the frame the run was handed is all it gets.
    machine: MachinePort | None = None
    # A grant changed under the running step: asked before each tool
    # dispatch, it tells the frame what Postgres holds now. A CLOSURE operations wires for a Machine
    # frame on the device, on its own short sessions; None everywhere else - no closure, no ask.
    regrant: Callable[[], Awaitable[None]] | None = None
    # Set ONLY on the conversation reply lane: it flips `definition_for` into the
    # chat compile (no frame tools, `propose_task` granted, CHAT_LAW appended) and rides the
    # meter so a reply's spend lands on the ledger attributed to its conversation. It is the
    # ONE flag that means talk: a bot talks in its conversation and nowhere else,
    # so a task-lane build - express step included - can never compile a bot to TALK.
    conversation_id: ConversationId | None = None
    # A task fact: the reply lane's answer to someone who is not the bot's owner. The ONLY prompt
    # text that anchors `web_fetch` is then the bot's instructions and the owner's own lines, so a
    # stranger's line never names a URL the bot reads (exfiltration by query string). Set by
    # operations from who spoke; None - the run's own prompts anchor, as on every task.
    fetch_anchor: str | None = None

    # How much of the web this step may read, from its task's size (`schemas.sizing.WEB_EFFORT`):
    # it lowers the search and fetch mounts' per-step caps. Set by the step runner's `equip`, and
    # by the express answer when its turn takes no web call; None elsewhere - the deployment's caps.
    web_effort: WebEffort | None = None
    # The run's wall, a `time.monotonic()` instant: its web tools never wait past it less the
    # time the model needs to answer (`ai/tools/wall.py`). Set by the bounded callers - the step
    # runner, the express answer, a conversation reply - once the wall is known (the tools read
    # it at call time); None everywhere else - the tools' own timeouts.
    deadline: float | None = None

    # The frame paths the system put in `/task/inputs/`: `publish` defaults an artifact's `sources`
    # to them, so a model that forgets to declare its sources never makes grounding vacuous.
    inputs: tuple[str, ...] = ()

    skill_pins: list[SkillPin] = field(
        default_factory=list[SkillPin]
    )  # the skill versions the plan pinned for this step
    # The part the step plays in a lane (a scatter map, a gather seat): the agent takes that part's mode
    # (`base.Mode`). Set ONLY by the step runner from the plan, which a model never writes; None - no mode.
    lane_role: LaneRole | None = None
    # The skills a frameless run may read (`skill_read`), frozen by operations for this run; empty - no tool.
    skill_menu: tuple[SkillCard, ...] = ()
    # The person's own memory, searched by a frameless run (`memory_search`): a CLOSURE operations wires
    # only where the work may read it (never in a Project); None - no tool.
    memory_search: SearchMemory | None = None

    # The connectors this run may reach: frozen specs, one `SecretStr` each. Written only by the step attempt
    # (`execution.equip`) and the Lead's express rung (its reads only), so the light agents never mount one.
    connectors: tuple[ConnectorSpec, ...] = ()
    # Nobody can answer a gate: a routine's run (its turn 1). A tainted run's connector writes then
    # run instead of asking (ADR-089); an explicit `ask` policy still asks.
    unattended: bool = False
    # Whose text the run's prompt and inputs already carry from the task's other steps: its taint
    # watermark starts here (`capabilities/taint.RunTaint`). Set only by the step attempt.
    taint_floor: Taint = Taint.TRUSTED
    # The facts intake computed for the turn a Lead run decides: its endings are refused against them
    # (`lead_check.LeadEndings`). Set only on a Lead run; every other seat has no turn to decide.
    lead_facts: LeadFacts | None = None
    # What a Lead's plan is checked and priced against (`lead_check.PlanCheck`), its seats included: set beside
    # `lead_facts`; None - no seat, skill or price, so every agent is refused and nothing is priced.
    plan_facts: PlanFacts | None = None
    # A frontier boundary's re-plan: the agent takes its replan seat (`AgentDef.replan`). Set only by operations.
    replanning: bool = False

    # The bots in this task's roster - user-authored agents, as frozen values.
    # `ai/agents/bot_compile.compile_bot` turns one into the `AgentDef` that `build()` runs; the compile
    # happens up in `agents` because `deps` sits BELOW it in the engine contract and may not hold one.
    #
    # Populated ONLY by `execution._attempt` and the planning path, exactly like `connectors`
    # above - so the light agents (judge, namer, memory_extract) can never
    # be served by a bot. That is what keeps a user from authoring a friendlier judge: not a
    # check, but the fact that the value never reaches the call that would build one.
    bots: tuple[ResolvedBot, ...] = ()
