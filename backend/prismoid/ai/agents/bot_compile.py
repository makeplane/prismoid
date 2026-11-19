# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bot compiler: a `ResolvedBot` value becomes an `AgentDef`, purely.

A built-in is a Python file that calls `register()` at import; a bot is a row compiled here, per
task, and handed to `build()` on `TaskDeps`. A bot **never enters `REGISTRY`**: a process-global
dict shared by every tenant is no place for one user's authored text. It lives beside `base.py`
because it produces an `AgentDef`, which sits above the bot format.

Four properties, each defended in code:

1. **A built-in always wins a name collision.** The reserved names (`manifest_refusal`) and the
   live `REGISTRY` are refused here, and at create time by `operations/bots.py`.
2. **The verifier suite is derived, never authored**: it follows `produces` through
   `ARTIFACT_TYPES`, the mapping every built-in obeys.
3. **A bot cannot be less disciplined than a built-in**: a compiled bot would pass
   `tests/laws/test_agent_registry.py`.
4. **A bot narrows the agent it runs on, never widens it** (`overlaid`): in its conversation it
   runs on the talk agent's hands, in a task on the step seats' hands; a grant no base holds
   mounts nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from prismoid.ai.agents import grants
from prismoid.ai.agents.base import REGISTRY, AgentDef, roster
from prismoid.ai.bots.format import manifest_refusal
from prismoid.ai.tools import catalog
from prismoid.ai.tools.ask import CONNECT_GRANT
from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.conversation import CHAT_LAW
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ARTIFACT_TYPES
from prismoid.schemas.plan import Seat

# The grants a run loops on: a bot holding one rides the run guard like a built-in with hands.
HANDS: Final = ("frame.", "web.")


# **What the system does with the run's output, told to the bot by the system.**
#
# A bot that holds no publish grant is on the text lane: its response IS the document and
# the engine writes it. The built-in `writer` knows this because its prompt says so in three
# paragraphs - a bot author has no way to know it, and the step's acceptance line (shared with
# every publishing agent) still reads "written to /work and then published with the publish
# tool". A live run found the exact failure that gap produces: the bot got the whole review
# done, could not find a publish tool, and spent its question budget asking for one.
#
# So the engine states its own fact rather than hoping the author guessed it - the same rule
# `TaskDeps.inputs` follows for `sources`: the system already knows this, and asking a model to
# repeat it is asking it to be wrong about it.
DELIVERY_LAW = """

---

## Delivering - your answer IS the deliverable

The system takes exactly what you return, names the file after its title, and publishes it.
There is no publish step, no file to write, and no tool to call - you have not been given one,
and you do not need one. Ignore any instruction to "write to /work and publish"; that is the
generic wording, and for you the system does it.

- Respond with the **complete deliverable and nothing else**: no "Here is the review:" preamble,
  no closing remarks, no narration about what you did. Your first line is the document's first line.
- Start with a `# Title` heading - it names the artifact.
- Never stop early, never summarise yourself, and never ask permission to deliver. One response,
  the whole thing.
"""


class BotCompileError(ValueError):
    """A bot that cannot legally become an agent (a stale row, or a bug): the caller drops it from
    the roster rather than running it."""


class BotTarget(StrEnum):
    """Where a bot runs: its conversation, or a step of a task."""

    TALK = "talk"
    TASK = "task"


# Connector reach from narrowest to widest.
_REACH: Final[tuple[grants.ConnectorReach, ...]] = ("none", "reads", "all")


@dataclass(frozen=True)
class BotBase:
    """What the agent a bot runs on holds: its tools, its connector reach, whether it may ask."""

    tools: frozenset[str]
    connectors: grants.ConnectorReach
    can_ask_user: bool


def overlaid(base: Collection[str], wanted: Sequence[str]) -> tuple[str, ...]:
    """The `wanted` grants `base` holds, in grant order, each once."""
    return tuple(dict.fromkeys(tool for tool in wanted if tool in base))


def base_of(target: BotTarget) -> BotBase:
    """The talk row's hands for a reply (`grants.TALK`), which never asks; for a task step, what any step
    seat holds."""
    if target is BotTarget.TALK:
        return BotBase(frozenset(grants.granted(grants.TALK)), grants.TALK_CONNECTORS, False)
    seats = [seat for seat in roster() if seat.plannable]
    reach = _REACH[max((_REACH.index(seat.connectors) for seat in seats), default=0)]
    return BotBase(
        frozenset(tool for seat in seats for tool in seat.tools), reach, any(s.can_ask_user for s in seats)
    )


def compile_bot(bot: ResolvedBot, *, conversation: bool = False) -> AgentDef:
    """One `ResolvedBot` to one `AgentDef` on its base, purely: `conversation=True` is the ONE talk
    compile (a bot talks in its conversation and nowhere else), every other is a task step."""
    _refuse(bot)
    target = BotTarget.TALK if conversation else BotTarget.TASK
    base = base_of(target)
    manifest = bot.manifest
    name = str(manifest.name)
    can_ask = base.can_ask_user and manifest.can_ask_user
    # The newborn arm: a bot with no instructions or description yet still chats and renders
    # sanely; publish demands both before a bot can reach a roster.
    instructions = bot.instructions.strip() or (
        f"You are {name}, a bot whose owner has not written instructions yet. Help with what "
        "you are asked, plainly and honestly, and say so when something needs judgment your "
        "owner has not yet given you. Corrections in this conversation are teaching: follow them."
    )
    description = manifest.description or (
        f"A new bot named {name}; its owner has not described it yet. Not for routing."
    )
    if conversation:
        return AgentDef(
            name=AgentName(name),
            model_class=manifest.capability,
            description=description,
            prompt=instructions + CHAT_LAW,
            output_ref=None,  # a reply is prose
            tools=overlaid(base.tools, (*manifest.tools, *grants.CARD_VERBS)),  # it proposes, never starts
            connectors=base.connectors,
            skills=(),  # a skill is a frame bundle; there is no frame here
            produces=manifest.produces,
            publishes_output=False,  # nothing a reply says is a deliverable
            verifier_suite=None,
            exclude_family=None,
            can_ask_user=False,  # a reply asks in prose: there are no forms in a chat
            plannable=False,  # the chat compile is never a step's vocabulary
        )
    # Who publishes: without `frame.publish` the run's output IS the document and the SYSTEM
    # publishes it (`_refuse` keeps that lane to documents), told so by DELIVERY_LAW.
    publishes_output = "frame.publish" not in manifest.tools
    tools = overlaid(base.tools, (*manifest.tools, *((CONNECT_GRANT,) if can_ask else ())))
    return AgentDef(
        name=AgentName(name),
        model_class=manifest.capability,
        description=description,
        # The author's words unchanged, with the machine's contract appended below them.
        prompt=instructions + (DELIVERY_LAW if publishes_output else ""),
        output_ref=None,  # prose or tool-published bytes - a bot never declares a typed output
        tools=tools,
        connectors=base.connectors,
        skills=tuple(manifest.skills),
        produces=manifest.produces,
        publishes_output=publishes_output,
        # Derived, never authored: a bot cannot choose the grader that will judge it.
        verifier_suite=ARTIFACT_TYPES[manifest.produces].verifier_suite,
        exclude_family=None,  # a verification concern, decided at verify time - not an author's
        can_ask_user=can_ask,
        plannable=True,  # the whole point: a bot is vocabulary the Lead can put in a step
        # A bot that loops on a sandbox or the web gets the brakes a built-in with hands gets.
        guarded=any(t.startswith(HANDS) for t in tools),
    )


def seatable(bots: Sequence[ResolvedBot]) -> list[AgentDef]:
    """The author's bots a plan may seat, first of a name wins; a bot that cannot compile is not a seat."""
    seated: dict[str, AgentDef] = {}
    for bot in bots:
        try:
            seated.setdefault(str(bot.manifest.name), compile_bot(bot))
        except BotCompileError:
            continue
    return list(seated.values())


def seat_table(bots: Sequence[AgentDef]) -> tuple[Seat, ...]:
    """Every agent a plan may seat, as the plan rules read it: the plannable built-ins, then `bots`."""
    return (*(_seat(a) for a in roster(plannable=True)), *(_seat(b, bot=True) for b in bots))


def _seat(agent: AgentDef, *, bot: bool = False) -> Seat:
    """`agent` as the plan rules read it."""
    gather = next((m.model_class for m in agent.modes if m.lane_role == "gather"), agent.model_class)
    return Seat(
        name=str(agent.name),
        model_class=agent.model_class,
        seat_class=gather,
        tools=tuple(sorted({catalog.model_name(tool) for tool in agent.tools})),
        kinds=agent.kinds,
        makes_formats=agent.makes_formats,
        publishes=agent.publishes,
        delivers_outcome=agent.delivers_outcome,
        bot=bot,
    )


def _refuse(bot: ResolvedBot) -> None:
    """Raise `BotCompileError` naming the first rule `bot` breaks: a built-in's name, or the format
    door's rules."""
    name = str(bot.manifest.name)
    if AgentName(name) in REGISTRY:
        raise BotCompileError(
            f"{name!r} is a built-in agent; a bot may not shadow one. Pick another name, for example '{name}-helper'."
        )
    if refusal := manifest_refusal(bot.manifest):
        raise BotCompileError(str(refusal)) from refusal
