# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Tool families mounted on a build, one row each, walked in order by `build()` (`MOUNTS`).

A family is one function over a `Mount` (what the build knows: the definition, the deps, the
catalog, the meter) returning a `Mounted` (the toolsets and run capabilities it adds), or
`NOTHING`, and the catalog tools it serves. Its row mounts it only when the definition holds one of
them (`grants.py` is where a built-in's hands are written); a row that serves no tool adds run
capabilities to every build. Adding a family is one function and one row in `MOUNTS`, never another
block in `build()`. The order of `MOUNTS` is the order toolsets reach the model and capabilities
wrap the run: `Steering` before `RunGuard` before `GrantFence`, so a steer holds a call before the
guard looks at it, and a call either of them stops never asks.

Only an agent that can park on a gate holds a connector's ask-state tool (`allow_ask`): on everyone
else those tools are absent, so `ApprovalRequired` is reachable exactly where the output union
already carries `DeferredToolRequests`.

A family that cannot work is absent, never broken: no grant, no closure or an empty chain means no
toolset, and the tool does not exist for this agent.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AgentCapability
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import AbstractToolset

from prismoid.ai import config
from prismoid.ai.capabilities import taint
from prismoid.ai.capabilities.compaction import Compaction
from prismoid.ai.capabilities.durability import detached
from prismoid.ai.capabilities.grant_fence import GrantFence
from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.capabilities.machine_acquire import MachineAcquire
from prismoid.ai.capabilities.steering import Steering
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.layout import ANSWERS, INPUTS, THREAD
from prismoid.ai.gateway import throttle
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import MeterContext, flat_emitter, units_emitter
from prismoid.ai.gateway.providers import api_key_for
from prismoid.ai.gateway.resolve import chain_for
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.skills.loader import read_body
from prismoid.ai.tools import catalog
from prismoid.ai.tools.browser import browser_toolset
from prismoid.ai.tools.chat import CHAT_TOOLS, chat_toolset
from prismoid.ai.tools.connectors import connector_toolsets
from prismoid.ai.tools.fetch import fetch_toolset
from prismoid.ai.tools.memory import memory_toolset
from prismoid.ai.tools.proposals import proposals_toolset
from prismoid.ai.tools.providers.images import ImageCandidate, image_toolset
from prismoid.ai.tools.providers.search import (
    SearchCaps,
    SearchEngine,
    price_per_query,
    search_chain,
    search_toolset,
)
from prismoid.ai.tools.reading import PageReading, Reader, reading_prompt
from prismoid.ai.tools.research import Shelf, research_toolset
from prismoid.ai.tools.routines import routines_toolset
from prismoid.ai.tools.skills import skills_toolset
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.sizing import lowered
from prismoid.schemas.usage import Unit

if TYPE_CHECKING:
    from prismoid.ai.agents.base import AgentDef
    from prismoid.ai.agents.run import AgentRunner


@dataclass(frozen=True)
class Mount:
    """What a build knows when it mounts a family."""

    definition: AgentDef
    deps: TaskDeps
    catalog: list[ModelSpec]
    meter: MeterContext
    served: ModelSpec | None  # the one row serving the run, or None for a mixed fallback chain
    order: dict[str, list[str]] | None
    quality: str | None
    # `base.build()` itself, bound to this build's catalog: how a tool builds the agent it runs on.
    build: Callable[[str, TaskDeps], AgentRunner] | None = None
    # The run's live tap restart (`TapSlot.restart`), when the run streams: a redirect tells it.
    restart: Callable[[], Awaitable[None]] | None = None

    @functools.cached_property
    def steering(self) -> Steering | None:
        """The build's one set of safe points, or None: only a step run carries an inbox."""
        if self.deps.steering is None:
            return None
        return Steering(self.deps.steering, can_park=self.definition.can_ask_user, on_redirect=self.restart)


@dataclass(frozen=True)
class Mounted:
    """What a family adds to the build."""

    toolsets: tuple[AbstractToolset[TaskDeps], ...] = ()
    capabilities: tuple[AgentCapability[TaskDeps], ...] = ()


NOTHING: Final = Mounted()

_JOURNAL: Final = {tool.name: tool.journal for tool in catalog.TOOLS}


@dataclass(frozen=True)
class Family:
    """One row of `MOUNTS`: what the family adds, and the catalog tools it serves (none: every build)."""

    adds: Callable[[Mount], Mounted]
    serves: tuple[str, ...] = ()

    def __call__(self, m: Mount) -> Mounted:
        """What the family adds to this build, or `NOTHING` when the definition holds none of its tools."""
        if self.serves and not any(_JOURNAL[tool] in m.definition.tools for tool in self.serves):
            return NOTHING
        return self.adds(m)


def _tools(*toolsets: AbstractToolset[Any] | None) -> Mounted:
    present = tuple(t for t in toolsets if t is not None)
    return Mounted(toolsets=present) if present else NOTHING


def connectors(m: Mount) -> Mounted:
    """Connector toolsets, prefixed MCP servers journaled like every tool, held as the definition's
    `connectors` says: all of a task's tools, the reads alone, or none of them."""
    deps = m.deps
    if not deps.connectors or m.definition.connectors == "none":
        return NOTHING

    served = connector_toolsets(
        tuple(sorted(deps.connectors, key=lambda spec: spec.slug)),  # their instructions, in slug order
        deps.journal_tool,
        approve=_approvals(m),
        allow_ask=m.definition.can_ask_user,
        reads_only=m.definition.connectors == "reads",
    )
    return _tools(*(toolset.prepared(_by_name) for toolset in served))


def _approvals(m: Mount) -> Approvals:
    """The run's approval rules: whether it has read outside text, whether its seat can ask, whether anyone
    is there to answer, and the teammate steer the build's `Steering` has told the run."""
    steering = m.steering
    return Approvals(
        tainted=taint.tainted,
        can_ask=m.definition.can_ask_user,
        attended=not m.deps.unattended,
        borrowed=(lambda: False) if steering is None else steering.borrows,
    )


def _by_name(_ctx: RunContext[TaskDeps], tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """A server's tools in name order, never the order it listed them: the cached prefix holds."""
    return sorted(tools, key=lambda tool: tool.name)


def web_fetch(m: Mount) -> Mounted:
    """The web door: a worker-process tool, frame egress bypassed by design; effort lowers its fetch cap, and
    `find_in_page` and `note` ride with it (a citing definition sees each page's source id)."""
    effort = m.deps.web_effort
    per_step = None if effort is None else effort.fetches
    deps = m.deps
    # The run's web state: the pages it reads, kept in the source store, each with its source id.
    shelf = Shelf(deps.keep_page, cites=m.definition.cites, ids={p: sid for sid, p in deps.sources.items()})
    door = fetch_toolset(
        m.definition.tools,
        deps.journal_tool,
        per_step=per_step,
        deadline=lambda: deps.deadline,
        inputs=INPUTS,
        own_words=(THREAD, ANSWERS),
        said=deps.fetch_anchor,
        reader=_page_reader(m),
        shelf=shelf,
    )
    if door is None:
        return NOTHING
    return _tools(door, research_toolset(m.definition.tools, shelf, deps.journal_tool))


def _beside(m: Mount, name: str) -> Callable[[str], Awaitable[Any]] | None:
    """A cheap agent's run beside the step: built like any agent so every call is metered, on deps that
    stream nothing, take no steer, hold no connector and run off the step's call log."""
    if (build := m.build) is None:
        return None
    deps = dataclasses.replace(m.deps, emit_event=None, steering=None, spend=None, connectors=())
    runner = functools.cache(lambda: build(name, deps))  # built once, at the first call

    async def run(prompt: str) -> Any:
        with detached():
            result = await runner().run(prompt)
        return result.output

    return run


def _page_reader(m: Mount) -> Reader | None:
    """`view="extract"`'s reader: the page reader, beside the step."""
    if (run := _beside(m, "page_reader")) is None:
        return None

    async def read(goal: str, text: str) -> PageReading:
        return await run(reading_prompt(goal, text))

    return read


def browser(m: Mount) -> Mounted:
    """The browser family, the web door's posture; `deps.browser` is None outside a browser-profiled frame,
    so a task-lane step never mounts one."""
    if m.deps.browser is None:
        return NOTHING

    hands = browser_toolset(m.definition.tools, m.deps.journal_tool, m.deps.browser, _approvals(m))
    return _tools(hands)


def steering(m: Mount) -> Mounted:
    """The safe points: only a step run carries an inbox; a pause parks the run only when its output
    type admits a deferral, else the step pauses at the frontier."""
    return NOTHING if m.steering is None else Mounted(capabilities=(m.steering,))


def run_guard(m: Mount) -> Mounted:
    """The run guard: budget and stall brakes at the same safe points, for a
    definition that opts in. Any other step agent (a fan-out seat - only a step run wires
    `deps.spend`) gets the budget's hold alone: a wide wave must not spend on past its cap."""
    if not m.definition.guarded:
        return NOTHING if m.deps.spend is None else Mounted(capabilities=(RunGuard(budget_only=True),))

    return Mounted(
        capabilities=(
            RunGuard(can_park=m.definition.can_ask_user, outcome=m.definition.output_ref == "outcome"),
        )
    )


def grant_fence(m: Mount) -> Mounted:
    """A grant changed under the running step. Only a Machine frame on
    the device carries the closure."""
    if m.deps.regrant is None:
        return NOTHING

    return Mounted(capabilities=(GrantFence(m.deps.regrant),))


def routines(m: Mount) -> Mounted:
    """Standing work proposed from inside a task: the tool's hands are a closure
    operations injected (deps.propose_routine - the emit_usage precedent); no closure, no tool. The
    routine is born PAUSED; the owner's resume is the arming ceremony."""
    if m.deps.propose_routine is None:
        return NOTHING

    return _tools(routines_toolset(m.deps.journal_tool, m.deps.propose_routine))


def skills(m: Mount) -> Mounted:
    """`skill_read` over the menu operations froze for this run, each body read from the skill store by
    its hash; an empty menu is no tool."""
    return _tools(skills_toolset(m.deps.skill_menu, read_body, m.deps.journal_tool))


def memory(m: Mount) -> Mounted:
    """`memory_search` over the closure operations wired where the person's memory may be read; none, no tool."""
    return _tools(memory_toolset(m.deps.memory_search))


def proposals(m: Mount) -> Mounted:
    """Work proposed from inside a CONVERSATION: the routines hook's twin. The bot can
    propose a task; it can never start one - the closure records a card, the door decides. Absent
    closure ⇒ absent tool, so a task-lane build never grows it."""
    if m.deps.propose_task is None:
        return NOTHING

    return _tools(proposals_toolset(m.deps.propose_task))


def chat(m: Mount) -> Mounted:
    """The card verbs of a conversation reply: present only while the reply lane wired them, so a
    task-lane build never grows them."""
    if m.deps.chat is None:
        return NOTHING

    granted = [name for name in CHAT_TOOLS if _JOURNAL[name] in m.definition.tools]
    return _tools(chat_toolset(m.deps.chat, granted))


def compaction(m: Mount) -> Mounted:
    """The history's bound (`capabilities/compaction.py`) on every build, the summarizer beside the
    step writing its handoff."""
    bound = Compaction.for_seat(m.definition.model_class, m.catalog, m.served, _beside(m, "summarizer"))
    return Mounted(capabilities=(bound,))


def search_tools(tools: tuple[str, ...], deps: TaskDeps, ctx: MeterContext) -> FunctionToolset[Any] | None:
    """`web_search` over the deployment's chain, each engine metered on `ctx`; the step's web effort
    lowers the per-step cap, never raises it."""
    cfg = config.current().search
    cap = lowered(cfg.max_per_step, None if deps.web_effort is None else deps.web_effort.searches)
    chain: list[SearchEngine] = []
    for ready in search_chain():
        chain.append(
            SearchEngine(
                name=ready.name,
                api_key=ready.key,
                base_url=ready.base,
                model=ready.model,
                emit_search=flat_emitter(
                    provider=ready.name,
                    item="web-search",
                    family="web",  # its own line in every by-family spend chart - honest
                    unit=Unit.SEARCH,
                    unit_price_usd=price_per_query(cfg, ready),
                    emit=deps.emit_usage,
                    ctx=ctx,
                ),
            )
        )
    return search_toolset(
        tools,
        deps.journal_tool,
        tuple(chain),
        SearchCaps(timeout_s=cfg.timeout_s, results=cfg.results, max_per_step=cap),
        deadline=lambda: deps.deadline,
    )


def web_search(m: Mount) -> Mounted:
    """The search lane: an empty chain is no toolset, and `web_search` does not exist."""
    return _tools(search_tools(m.definition.tools, m.deps, m.meter))


def media(m: Mount) -> Mounted:
    """The media lane. `tools` sits BESIDE `gateway` in the engine contract, so this is
    where the two meet: resolve the `image-gen` chain from the same catalog/routing data as any
    class, bind one usage-emitter per candidate, flatten to values the sibling layer may
    hold, and hand the tool the per-provider throttle. No enabled image model ⇒ the toolset is None
    and `generate_image` simply does not exist for this agent - absent beats broken."""
    image_specs = chain_for(
        ModelClass.IMAGE_GEN,
        m.catalog,
        order=(m.order or {}).get(ModelClass.IMAGE_GEN.value),
        quality=m.quality,  # economy leads with grok-imagine, max with gpt-image - same dial
    )
    candidates = tuple(
        ImageCandidate(
            provider=spec.provider,
            model_id=spec.model_id,
            protocol=str(spec.protocol),
            base_url=spec.base_url,
            api_key=api_key_for(spec.provider),
            emit_units=units_emitter(spec, m.deps.emit_usage, m.meter),
        )
        for spec in image_specs
    )
    return _tools(
        image_toolset(
            m.definition.tools, m.deps.frame, m.deps.journal_tool, candidates, throttled=throttle.throttled
        )
    )


MOUNTS: Final[tuple[Family, ...]] = (
    Family(connectors),  # open vocabulary: held by the definition's `connectors`
    Family(web_fetch, ("web_fetch",)),
    Family(browser, tuple(tool.name for tool in catalog.family("browser"))),
    Family(steering),
    Family(run_guard),
    Family(grant_fence),
    Family(lambda _: Mounted(capabilities=(MachineAcquire(),))),  # inert without `deps.machine`
    Family(routines, ("routine_create",)),
    Family(proposals, ("propose_task",)),
    Family(chat, CHAT_TOOLS),
    Family(skills, ("skill_read",)),
    Family(memory, ("memory_search",)),
    Family(web_search, ("web_search",)),
    Family(media, ("generate_image",)),
    Family(compaction),
)
