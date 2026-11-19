# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The engine seam. `AgentDef` + `register()` + `REGISTRY` + `build()`.

**Adding an agent is one file, one prompt, and one import in `all.py`** - and it is then live in
the registry, in the Lead's vocabulary, in `GET /v1/agents`, and in the parametrized CI test.
Nothing else is touched. That is the entire point of this file.

**Agents are roles, never models**. `writer`, `maker`, `researcher` - a *job title*. What
model serves that role is a routing decision made from the catalog at run time, which is why
an `AgentDef` names a `ModelClass` and never a model string.

**An agent is data**: its instruction blocks (the prompt's `##` sections), its model class, its grants
(`grants.py`), the capabilities it adds, its output type. `build()` renders the blocks stable first
(the identity every seat shares, the role, the device facts) and records their ids as `prompt_version`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic_ai import Agent, DeferredToolRequests, ToolOutput
from pydantic_ai.capabilities import AgentCapability
from pydantic_ai.models import Model
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, WrapperToolset

from prismoid.ai import config
from prismoid.ai.agents import grants
from prismoid.ai.agents.mounts import MOUNTS, Mount, Mounted
from prismoid.ai.agents.run import AgentRunner, TapSlot
from prismoid.ai.capabilities.durability import PrismoidDurability
from prismoid.ai.capabilities.output_checks import OUTPUT_RETRIES, OutputChecks
from prismoid.ai.capabilities.taint import RunTaint
from prismoid.ai.capabilities.tool_record import ToolRecorder
from prismoid.ai.deps import TaskDeps  # runtime, not TYPE_CHECKING: Agent(deps_type=TaskDeps)
from prismoid.ai.frames.layout import STANDARD_PATHS, FramePaths, render
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import MeterContext
from prismoid.ai.gateway.providers import ClassSettings, default_model_settings
from prismoid.ai.gateway.resolve import resolve
from prismoid.ai.skills import deliverables
from prismoid.ai.tools.ask import CONNECT_GRANT, with_ask_user, with_request_connector
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.schemas import outputs
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import AgentName, SkillName
from prismoid.schemas.machine_profile import widen
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import ArtifactType, VerifierSuite
from prismoid.schemas.plan import LaneRole


@dataclass(frozen=True, slots=True)
class Block:
    """One named part of a seat's instructions: a prompt's `##` section, exactly as the model reads it."""

    id: str  # `<source>.<heading slug>`; the text before a source's first heading is `<source>`
    text: str


def blocks_of(text: str, source: str) -> tuple[Block, ...]:
    """`text` cut before each `##` heading; the blocks' texts join back to `text` exactly."""
    pieces: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## ") or not pieces:
            pieces.append(line)
        else:
            pieces[-1] += line
    return tuple(Block(_block_id(piece, source), piece) for piece in pieces)


def _block_id(piece: str, source: str) -> str:
    """The block's id: its source, then its heading's words joined by dashes when it has one."""
    if not piece.startswith("## "):
        return source
    heading = piece[3:].partition("\n")[0].lower()
    return f"{source}." + "-".join("".join(c if c.isalnum() else " " for c in heading).split())


@dataclass(frozen=True, slots=True)
class Mode:
    """A part a step agent plays in a lane, taken when its step's lane role is `lane_role`: a seat that cannot ask,
    with the part's typed answer, model class and craft (`_moded`)."""

    lane_role: LaneRole
    output_ref: str  # the typed answer the system publishes and the lane expands or tallies
    model_class: ModelClass
    craft: str  # the part's instruction blocks, after the role's own
    guarded: bool = False  # one looping seat keeps the run guard; a wide wave's seats rotate per attempt
    capabilities: tuple[AgentCapability[TaskDeps], ...] = ()  # in place of the agent's own
    quiet_stream: bool = (
        True  # the seat's `AgentDef.quiet_stream`: a wave stays quiet, a lane's one deliverable streams
    )


@dataclass(frozen=True, slots=True)
class AgentDef:
    """One agent, declared. Frozen: a registry you can mutate at run time is not a registry."""

    name: AgentName
    model_class: ModelClass
    description: str  # what it is for, in the vocabulary the Lead reasons in

    prompt: str = ""  # loaded from prompts/<name>.md
    output_ref: str | None = None  # a key in schemas/outputs.py; None ⇒ Narration
    # A seat that may end several typed ways (the Lead): its whole output type, text and output tools, in
    # place of `output_ref`; it never widens to a deferral, since its question is one of its endings.
    endings: tuple[type[str] | ToolOutput[Any], ...] = ()
    # The reasoning effort this seat is asked with, over its class's routing settings: only where the
    # design sets one for the role (the Lead decides on `low` where its class's other seats think more).
    effort: ThinkingEffort | None = None
    # Granted by the record's names; an ungranted tool is not mounted. A built-in's come from its
    # `grants.GRANTS` row at `register()`, a bot's from its manifest.
    tools: tuple[str, ...] = ()
    connectors: grants.ConnectorReach = "none"  # the task's connector tools it holds, the same way
    skills: tuple[SkillName, ...] = ()  # default skill pins; the plan may add more

    # What this agent's published deliverable IS. The plan rules type every publishing step's
    # contract from this (via the roster mapping - the rules stay below the registry), so a fleet table
    # step's contract says `fleet_report` and the verify judge grades against the truth. Before
    # this field, every contract said `document` and three agents' contracts lied.
    produces: ArtifactType = ArtifactType.DOCUMENT
    # **Does this seat make every kind a format skill builds?** Then a step's kind is the plan's
    # choice among `kinds`, its craft is that kind's format skill, and the skill seats the run
    # (`_formatted`); `produces` and `verifier_suite` are left unset, each kind's suite checks its file.
    makes_formats: bool = False

    # **The run's OUTPUT is the deliverable, and the SYSTEM publishes it**. Typed
    # (`output_ref="collection_result"`) or text (`output_ref=None`, the writer): either way the
    # engine writes the canonical bytes and walks them through the one door after the run -
    # `publish` never sits in the model's hands, so forgot-to-publish and truncated-tool-args
    # are not failure modes this agent can have. False ⇒ the agent publishes through tools.
    publishes_output: bool = False

    verifier_suite: VerifierSuite | None = None
    # **Does it cite by source id?** Read before its run: the task's sources load for its step, each page it
    # reads is shown with an id (`[S3]`), and its publish renders each as the page's link. The check that sends
    # back an id no read was given is its `CitationCheck` capability.
    cites: bool = False
    # Only with a reason. The default - "anything but the producer's family" - is decided at
    # verification time, not here; this is for an agent that must *never* be served by a family.
    exclude_family: Family | None = None

    # `ask_user` is granted to **top-level step agents only**. A sub-agent escalates
    # through its parent's contract; nested deferral bubbling is a known library edge, and
    # pretending otherwise is how a task parks forever on a question nobody can see.
    can_ask_user: bool = False

    # Fan-out machinery (scatter seats): the model's prose NEVER rides the thread's
    # message lane - three parallel seats interleaving into one conversation is noise, and a
    # provider that echoes its typed output as text (grok did) paints
    # a wall of raw JSON where the answer belongs. Liveness stays on the activity lane (the
    # progress tree's thinking pour); the thread hears the system's own receipt instead.
    quiet_stream: bool = False

    # **Can a plan put this agent in a step?** The machinery cannot: the `lead` (which would happily
    # plan a step that plans the plan) and the `judge` (which verification resolves for itself,
    # cross-family, and which the plan must not be able to choose - a plan that picks its own judge
    # picks a friendly one).
    #
    # They are still real registry entries: they appear in `GET /v1/agents`, they are built through
    # `build()` like everything else, and the CI case that runs every agent runs them too. What they
    # are not is *vocabulary*. `roster(plannable=True)` is what the Lead is shown.
    plannable: bool = True

    # **Does this seat carry the identity?** The identity (prompts/_identity.md) is who the system
    # is in three lines plus the rules every seat keeps (the voice, the em-dash ban). It is
    # prepended at `build()`, so the definition's own `prompt` stays the role's text. The judge
    # declines it: its value is distance from the work. The page reader declines it too:
    # it copies a page's words, never speaks in the house voice.
    identity: bool = True

    frame_profile: str = "base"  # base | code | browser - the image the step's frame boots

    # **Does the run guard ride this seat?** The budget notices and park,
    # the stall brakes and the request ceiling at the steering safe points - and the retry that
    # carries the conversation and keeps one model. True for every top-level step agent that runs
    # a tool loop (the computer, the operator, the researcher, the writer, the
    # maker); a fan-out seat keeps its per-attempt rotation, a single-breath agent needs none.
    guarded: bool = False

    # **Does this seat also drive the browser on the device?** A coding
    # task there runs its server and tests it in the task's window, so the seat boots the browser
    # profile and holds the browser tools on a device task only (`frame_profile_for`, `_seated`).
    # `frame_profile` stays its own: it is still a publisher, never an outcome.
    device_browser: bool = False
    # **The craft of a step that drives the browser (`StepSpec.browser`)**, after the role's own; set, the agent
    # browses per step. That step's seat boots the browser image and holds the browser tools (`_browsing`); any
    # other step of it runs without a browser, a connector act on paper.
    browser_craft: str = ""

    # Run capabilities this agent adds to every build of it, after the mounted families' and before the
    # call log: a new per-run behaviour is a capability, never another flag here.
    capabilities: tuple[AgentCapability[TaskDeps], ...] = ()

    # The parts this agent plays in a deterministic lane, by the step's lane role (a scatter map, a gather
    # seat). The lane's expansion sets the role and a model never writes one, so no plan can choose a mode.
    modes: tuple[Mode, ...] = ()
    # The seat an agent that writes plans takes at a frontier boundary's re-plan (`TaskDeps.replanning`): its
    # narrower endings, effort and checks; None - the agent never re-plans.
    replan: AgentDef | None = None

    @property
    def blocks(self) -> tuple[Block, ...]:
        """The role's instruction blocks, cut from its prompt."""
        return blocks_of(self.prompt, str(self.name))

    @property
    def browses(self) -> bool:
        """Whether a step of this agent may drive the browser (`browser_craft`)."""
        return bool(self.browser_craft)

    @property
    def kinds(self) -> tuple[ArtifactType, ...]:
        """Every kind a step of this agent can publish: its `produces`, or each format skill's kind."""
        return deliverables.kinds() if self.makes_formats else (self.produces,)

    @property
    def publishes(self) -> bool:
        """Whether a step of this agent can publish at all: through the tool, or as the system publishing its output."""
        return self.publishes_output or "frame.publish" in self.tools

    @property
    def delivers_outcome(self) -> bool:
        """Whether a step of this agent delivers by FINISHING with `done(Outcome)`: what it did or found is the
        deliverable, and a file it publishes rides beside it (the operator). The plan never demands a publish from it."""
        return self.output_ref == "outcome"


REGISTRY: Final[dict[AgentName, AgentDef]] = {}

# The one shared identity (Plane, the AI factory; Prismoid, the worker it built) and the rules every
# seat keeps, said once; the seats that decline it keep their own copy of the style line.
IDENTITY: Final = (Path(__file__).parent / "prompts" / "_identity.md").read_text()

# The facts of the user's own Mac, after the role on every frame on the device and nowhere else:
# the limits a cloud frame's isolation never asks an agent to know.
DEVICE_LAW: Final = (Path(__file__).parent / "prompts" / "_device_law.md").read_text()


IDENTITY_BLOCKS: Final = blocks_of(IDENTITY, "identity")


def instruction_blocks(
    definition: AgentDef, paths: FramePaths = STANDARD_PATHS
) -> tuple[tuple[Block, ...], ...]:
    """A seat's texts as blocks, stable first: the identity every seat shares, the role, then on the device
    the device facts. Separated, never woven: a role's prompt file is exactly what that role adds."""
    texts = [IDENTITY_BLOCKS] if definition.identity else []
    texts.append(definition.blocks)
    if paths.on_host:
        texts.append(blocks_of(f"{DEVICE_LAW}{paths.also_granted()}", "device_law"))
    return tuple(texts)


def instructions_for(definition: AgentDef, paths: FramePaths = STANDARD_PATHS) -> str:
    """The system prompt a seat runs with: its texts with a rule between, spelled in the frame's own paths."""
    texts = ("".join(block.text for block in text) for text in instruction_blocks(definition, paths))
    return render("\n\n---\n\n".join(texts), paths)


def prompt_version(definition: AgentDef, paths: FramePaths = STANDARD_PATHS) -> str:
    """The ids of the blocks a seat runs with, in order; a run records it, so an answer names the blocks in force."""
    return " ".join(block.id for text in instruction_blocks(definition, paths) for block in text)


def register(agent: AgentDef) -> AgentDef:
    """Idempotent by name, with its hands from its grants row. A duplicate is a bug - two agents with one
    name means one of them is unreachable, and which one depends on import order."""
    if agent.tools or agent.connectors != "none":
        raise ValueError(f"agent {agent.name!r} declares its own tools; a built-in's live in grants.py")
    name = str(agent.name)
    agent = dataclasses.replace(
        agent, tools=grants.granted(name), connectors=grants.CONNECTORS.get(name, "none")
    )
    if agent.name in REGISTRY and REGISTRY[agent.name] != agent:
        raise ValueError(f"agent {agent.name!r} is already registered with a different definition")
    REGISTRY[agent.name] = agent
    return agent


_loaded = False


def _load() -> None:
    """Populate the registry, once. **The import IS the registration** (`all.py`).

    Without this, `REGISTRY` is empty in any process that never happened to import `all.py`, and
    `build("writer")` fails with `no agent named 'writer'; known: []` - a message that tells you
    the truth and still wastes twenty minutes, because the bug is *somewhere else entirely*: in
    whichever module forgot an import.

    **A registry you can forget to load is a registry that will be forgotten.** So it loads itself,
    lazily, at the first question anyone asks it. The import is safe here and not at module level:
    `all.py` imports this module, and by the time anyone calls `get()` this module is fully
    initialised.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    # `importlib`, not a bare `import`: the module is imported for its **side effect** (every agent
    # file registers itself on import), and a bare import of a module whose value is unused is a
    # lint error and a pyright error, in every codebase, forever. Saying what we mean is cheaper
    # than suppressing two tools that are both right.
    importlib.import_module("prismoid.ai.agents.all")


def find(name: str) -> AgentDef | None:
    """The built-in agent named `name`, or None."""
    _load()
    return REGISTRY.get(AgentName(name))


def get(name: str) -> AgentDef:
    if (definition := find(name)) is None:
        raise KeyError(f"no agent named {name!r}; known: {sorted(REGISTRY)}")
    return definition


def frame_profile_for(
    name: str, *, browser: bool = False, device: bool = False, makes: ArtifactType | None = None
) -> str:
    """The image a step of `name` boots: `browser` for a browsing step (or a `device_browser` seat the kill
    switch allows), else the definition's profile widened to its format skill's image (`base` for a bot)."""
    definition = find(name)
    if definition is None:
        return "base"
    if definition.browses and browser:
        return "browser"
    if device and definition.device_browser and config.current().computer_use.enabled:
        return "browser"
    made = deliverables.format_for(makes) if makes is not None and definition.makes_formats else None
    if device or made is None or made.frame_profile is None:
        return definition.frame_profile
    return widen(definition.frame_profile, made.frame_profile)


def roster(*, plannable: bool = False) -> list[AgentDef]:
    """The roster screen's content and `GET /v1/agents`'s body; `plannable=True` is the only list the Lead sees,
    so a step naming machinery (a `judge`) fails the plan rules as "no such agent"."""
    _load()
    return [REGISTRY[name] for name in sorted(REGISTRY) if REGISTRY[name].plannable or not plannable]


def definition_for(name: str, deps: TaskDeps) -> AgentDef:
    """The agent this name means **for this task** - a built-in, or one of its bots.

    Two lookups, in one deliberate order. `REGISTRY` is asked FIRST, so a built-in always wins a
    name collision: a user who could shadow `judge` could author a friendly grader, and a rule
    enforced by lookup order cannot be forgotten the way a validation can. `operations/bots.py`
    refuses the name at create time too - this is the belt behind that brace, and it is the one
    that holds if a built-in is added *after* a bot took its name.

    A bot that cannot legally compile is not silently skipped: `BotCompileError` propagates, the
    step fails honestly, and the reason names the rule. The roster builder is where a bad row
    gets excluded-and-logged; by the time we are constructing an agent, the plan already chose it.
    """
    if (definition := find(name)) is not None:
        return _seated(definition, deps)

    # Imported here, not at module level: `custom` imports `AgentDef` from this module, and the
    # one-way import at the top plus the lazy one here is how that cycle stays a straight line.
    # (`_load()` does the same thing for `all.py`, for the same reason.)
    from prismoid.ai.agents.bot_compile import compile_bot

    for bot in deps.bots:
        if str(bot.manifest.name) == name:
            # The conversation lane: the same bot, compiled to TALK - no frame, no
            # publish, the propose tool instead. Decided by the deps, so no caller can hand a
            # chat reply a sandbox by forgetting a flag - and by the ONE flag: a task
            # lane never carries a conversation id, so it can never compile a bot to talk.
            return compile_bot(bot, conversation=deps.conversation_id is not None)

    known = sorted(REGISTRY) + sorted(str(b.manifest.name) for b in deps.bots)
    raise KeyError(f"no agent named {name!r}; known: {known}")


def _seated(definition: AgentDef, deps: TaskDeps) -> AgentDef:
    """A built-in with what this step adds: a maker's format skill seat, a lane part's `Mode`, a
    `device_browser` seat's browser tools, a browsing seat; else the registry's own definition."""
    if deps.replanning and definition.replan is not None:
        return definition.replan
    if definition.makes_formats:
        definition = _formatted(definition, deps.skill_pins)
    if (mode := next((m for m in definition.modes if m.lane_role == deps.lane_role), None)) is not None:
        return _moded(definition, mode)
    if definition.browses:
        return browsing(definition, on=deps.browser is not None)
    if not definition.device_browser or deps.browser is None:
        return definition
    return dataclasses.replace(definition, tools=grants.granted(str(definition.name), grants.EVERYWHERE))


def browsing(definition: AgentDef, *, on: bool) -> AgentDef:
    """A browsing agent's seat for one step: on the browser, its craft after the role's and the hands its grants
    hold there (`grants` need `browser`); off it, the registry's own, so a connector act never takes a Machine."""
    if not on:
        return definition
    return dataclasses.replace(
        definition,
        prompt=f"{definition.prompt.rstrip()}\n\n{definition.browser_craft.strip()}\n",
        tools=grants.granted(str(definition.name), grants.BROWSING),
    )


def _moded(definition: AgentDef, mode: Mode) -> AgentDef:
    """A step agent in a lane part: a seat that cannot ask, holds a seat's tools and no connector, and answers
    with the part's typed output, which the system publishes; a wave's seat is quiet."""
    return dataclasses.replace(
        definition,
        model_class=mode.model_class,
        prompt=f"{definition.prompt.rstrip()}\n\n{mode.craft.strip()}\n",
        output_ref=mode.output_ref,
        publishes_output=True,
        tools=grants.seat_held(definition.tools),
        connectors="none",
        can_ask_user=False,
        quiet_stream=mode.quiet_stream,
        guarded=mode.guarded,
        capabilities=mode.capabilities,
    )


def _formatted(definition: AgentDef, pins: Sequence[SkillPin]) -> AgentDef:
    """A format maker seated by its step's format skill: that skill's model class, and for a kind the system
    writes from a typed answer, that answer as the run's output. No format skill pinned: the bare seat."""
    made = next((f for pin in pins if (f := deliverables.of_skill(pin.name)) is not None), None)
    if made is None:
        return definition
    typed = deliverables.TYPED.get(made.kind)
    if typed is None:
        return dataclasses.replace(definition, model_class=made.model_class)
    return dataclasses.replace(
        definition, model_class=made.model_class, output_ref=typed, publishes_output=True
    )


def build(
    name: str,
    deps: TaskDeps,
    catalog: list[ModelSpec],
    *,
    exclude_family: Family | None = None,
    order: dict[str, list[str]] | None = None,
    settings: Mapping[str, ClassSettings] | None = None,
    pin: str | None = None,
    quality: str | None = None,
) -> AgentRunner:
    """The ONLY agent constructor: model class resolved against the enabled catalog, granted tools
    mounted, capabilities in order; returns the runner every run starts from."""
    definition = definition_for(name, deps)
    meter = MeterContext(
        task_id=deps.task_id,
        step_id=deps.step_id,
        project_id=deps.project_id,
        conversation_id=deps.conversation_id,
    )
    live = TapSlot() if deps.emit_event is not None else None  # the run's live tap, when it streams
    checks = OutputChecks()  # what it leaves on a run's ending, the runner hands operations
    mount = Mount(
        definition=definition,
        deps=deps,
        catalog=catalog,
        meter=meter,
        served=next((m for m in catalog if m.ref == pin), None),
        order=order,
        quality=quality,
        build=lambda nested, nested_deps: build(nested, nested_deps, catalog, order=order, settings=settings),
        restart=None if live is None else live.restart,
    )
    # `deps.inputs` reaches the toolset so that `publish` stamps them as `sources` (`frame_tools.publish`).
    toolset = frame_toolset(definition.tools, deps.frame, deps.journal_tool, deps.inputs, deps.preview_tool)
    mounted = _mounts(mount)
    paths = deps.frame.paths if deps.frame else STANDARD_PATHS
    version = prompt_version(definition, paths)
    agent = Agent(
        _model(mount, exclude_family=exclude_family, pin=pin),
        deps_type=TaskDeps,
        output_type=_output_type(definition, toolset),
        instructions=instructions_for(definition, paths),
        name=str(definition.name),
        metadata={"prompt_version": version},
        toolsets=[toolset, *mounted.toolsets],
        # Prompt-cache breakpoints, the class's request wall clock (CLASS_TIMEOUT_S), effort and tier.
        model_settings=default_model_settings(
            definition.model_class,
            _asked(definition, settings),
            cache_key=cache_key(str(definition.name), version, [toolset, *mounted.toolsets]),
        ),
        # pydantic-ai's schema retry, not the ladder; the output checks' nudges ride on top.
        retries={"tools": 3, "output": OUTPUT_RETRIES},
        capabilities=_capabilities(definition, mounted, live, checks),
    )
    return AgentRunner(agent, deps, definition, live=live, checked=checks.checked)


def _asked(definition: AgentDef, settings: Mapping[str, ClassSettings] | None) -> ClassSettings | None:
    """The class's routing settings for this seat, its own effort over the class's."""
    chosen = (settings or {}).get(definition.model_class.value)
    if definition.effort is None:
        return chosen
    return dataclasses.replace(chosen or ClassSettings(), effort=definition.effort)


def cache_key(name: str, version: str, toolsets: Sequence[AbstractToolset[Any]]) -> str:
    """OpenAI's prompt-cache routing key: a digest of the static prefix alone (agent, prompt version,
    mounted tools), so every task running the same head shares a warm shard; the meter adds the model."""
    head = json.dumps([name, version, [_tool_heads(t) for t in toolsets]], sort_keys=True, default=str)
    return "prismoid-" + hashlib.sha256(head.encode()).hexdigest()[:32]


def _tool_heads(toolset: AbstractToolset[Any]) -> list[Any] | str:
    """What one toolset adds to the prefix: its tools' definitions by name, or its id when they come at run time."""
    while isinstance(toolset, WrapperToolset):
        toolset = toolset.wrapped
    if isinstance(toolset, FunctionToolset):
        tools = sorted(toolset.tools.items())
        return [[n, t.tool_def.description, t.tool_def.parameters_json_schema] for n, t in tools]
    return toolset.id or type(toolset).__name__


def _model(m: Mount, *, exclude_family: Family | None, pin: str | None) -> Model:
    """The metered, fallback-chained model serving the definition's class (a fleet seat's `pin`: exactly
    that ref); `exclude_family` keeps a verifier off the producer's family."""
    return resolve(
        m.definition.model_class,
        m.catalog,
        emit_usage=m.deps.emit_usage,
        context=m.meter,
        exclude_family=exclude_family or m.definition.exclude_family,
        order=m.order,  # the routing table's per-class chain; None: alphabetical fallback
        pin=pin,
        quality=m.quality,  # the requirement dial; only the step path passes one
    )


def _mounts(m: Mount) -> Mounted:
    """Every family's toolsets and run capabilities, in `MOUNTS` order."""
    mounted = [family(m) for family in MOUNTS]
    return Mounted(
        toolsets=tuple(t for each in mounted for t in each.toolsets),
        capabilities=tuple(c for each in mounted for c in each.capabilities),
    )


def _capabilities(
    definition: AgentDef, mounted: Mounted, live: TapSlot | None, checks: OutputChecks
) -> list[AgentCapability[TaskDeps]]:
    """The run's capabilities, outermost first: the record sees every call's real outcome (held,
    deferred and unknown included); the call log is innermost by the framework, not by place."""
    return [
        ToolRecorder(),
        RunTaint(),
        *mounted.capabilities,
        *definition.capabilities,
        checks,
        PrismoidDurability(event_stream_handler=live),
    ]


def _output_type(definition: AgentDef, toolset: FunctionToolset[Any]) -> Any:
    """The run's output type, and the ask tools on the frame toolset for an agent that may ask."""
    # **The output type widens to admit a question, but only for an agent allowed to ask**.
    # `DeferredToolRequests` is what the run returns when `ask_user` defers; without it in the output
    # union, pydantic-ai has nowhere to put the deferral and errors instead of pausing. A judge gets
    # neither the tool nor the type - a verifier that can ask the author for help is not a verifier.
    if definition.endings:
        return list(definition.endings)
    output_type: Any = outputs.get(definition.output_ref)
    # A named output tool: the computer finishes with `done`, so its prompt can say so.
    if (tool_name := outputs.output_tool_name(definition.output_ref)) is not None:
        output_type = ToolOutput(output_type, name=tool_name)
    if definition.can_ask_user:
        with_ask_user(toolset)
        # The connect block is a deferral too, so it rides the same widened output
        # type - but it is its own declared grant: a browser step asks for a login and is never
        # pointed at a connector its door already ruled out.
        if CONNECT_GRANT in definition.tools:
            with_request_connector(toolset)
        output_type = [output_type, DeferredToolRequests]
    return output_type


# The names the one-file-per-agent modules and the run's callers import from here.
__all__ = [
    "REGISTRY",
    "AgentDef",
    "build",
    "get",
    "register",
    "roster",
]
