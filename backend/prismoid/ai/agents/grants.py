# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Who holds which tool: one row per agent, keyed by the tool catalog's names (`catalog.TOOLS`).

The one source of an agent's hands: `register()` compiles a built-in's row into its `AgentDef.tools`
(the record's names, `granted`), and `build()` mounts only what that holds.

A grant that `needs` something mounts only where the deployment or the frame provides it (a search
engine, an image model, a browser on the person's device); every other grant mounts wherever its
agent runs, given the frame or the closure its family asks for. An agent with no row holds nothing.
A connector's tools are open vocabulary, attached per task: an agent in `CONNECTORS` holds the task's
whole inventory, or only the tools their servers label read-only; any other agent holds none of them.

Skills ride the same law. A step's pinned skill is honoured only where the step's agent holds every
tool the skill declares it needs (`allowed-tools`, `skill_fits`); a declaration grants nothing. A
frameless agent reads only the skills `SKILLS` grants it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from prismoid.ai.tools import catalog

# `browser`: a step that drives the task's browser (`StepSpec.browser`), the browsing agent's other seat.
Needs = Literal["search", "image_model", "device_browser", "browser"]
EVERYWHERE: Final[frozenset[Needs]] = frozenset({"search", "image_model", "device_browser", "browser"})
# What a definition carries everywhere: the device browser and a browser step are the seat's to add (`base._seated`).
DEPLOYED: Final[frozenset[Needs]] = frozenset({"search", "image_model"})
BROWSING: Final[frozenset[Needs]] = frozenset({"search", "image_model", "browser"})  # a browser step's

ConnectorReach = Literal["none", "reads", "all"]


@dataclass(frozen=True)
class Grant:
    """One tool an agent holds, what must be present for it to mount, and what takes it away (`off`)."""

    tool: str
    needs: Needs | None = None
    off: Needs | None = None


def _hold(*names: str, needs: Needs | None = None, off: Needs | None = None) -> tuple[Grant, ...]:
    """Grants for catalog tools, refusing a name the catalog does not hold."""
    catalog.known(*names)
    return tuple(Grant(name, needs, off) for name in names)


_BROWSER: Final = tuple(tool.name for tool in catalog.family("browser"))
_ASKS: Final = _hold("ask_user", "request_connector")
_MAKES: Final = _hold("read_file", "write_file", "edit", "shell", "publish")
_SEARCH: Final = _hold("web_search", needs="search")
_WEB: Final = (*_hold("web_fetch"), *_SEARCH)
_RESEARCH: Final = ("find_in_page", "note")
# A reply's own verbs: it proposes work and answers for its cards; a bot's conversation reply adds them too.
_CARDS: Final = _hold("propose_task", "continue_card", "accept_pending", "decline_pending")
CARD_VERBS: Final = tuple(catalog.tool(grant.tool).journal for grant in _CARDS)  # their record names

# The hands a Bot's conversation reply runs on (ADR-059), and the connector reach it has: a reply is
# no agent's, so its row stands beside the table.
TALK: Final = (*_hold("web_fetch", "skill_read", "memory_search"), *_CARDS, *_SEARCH)
TALK_CONNECTORS: Final[ConnectorReach] = "reads"

GRANTS: Final[Mapping[str, tuple[Grant, ...]]] = {
    # A cited page is opened to check a claim against it; the anchor admits only URLs the work names.
    "judge": _hold("web_fetch"),
    # Reads to decide a turn; its endings are its outputs, and its one record write is a paused routine.
    "lead": (*_hold("web_fetch", "find_in_page", "memory_search", "skill_read", "routine_create"), *_SEARCH),
    # One file built with code: files, the shell, asks and the web; an image model where the host has one, the
    # browser on the device.
    "maker": (
        *_MAKES,
        *_ASKS,
        *_WEB,
        *_hold("generate_image", needs="image_model"),
        *_hold(*_BROWSER, needs="device_browser"),
    ),
    # Files and the web on every step; the shell and the browser only on a step that drives the browser
    # (`base.browsing`), the connect block off it.
    "operator": (
        *(g for g in _MAKES if g.tool != "shell"),
        *_hold("ask_user"),
        *_hold("request_connector", off="browser"),  # a step told to connect first never opens the page
        *_WEB,
        *_hold("shell", *_BROWSER, needs="browser"),
    ),
    # Its answer is the findings, and the system publishes them: no file hands, so it never needs a Machine.
    "researcher": (
        *_hold("read_file", "web_fetch", "routine_create", *_RESEARCH),
        *_ASKS,
        *_SEARCH,
    ),
    # Reads the pages it cites and checks a figure on them; its draft is its memory, so no `note`.
    "writer": (*_hold("read_file", "web_fetch", "find_in_page", "routine_create"), *_ASKS, *_SEARCH),
}

# A fan-out seat's hands, whichever step agent it runs as (`base.Mode`): it reads and keeps notes. It never asks, proposes
# a routine or reaches a connector: parallel seats read untrusted pages and need no private data.
SEAT_TOOLS: Final = frozenset({"read_file", "web_fetch", "web_search", *_RESEARCH})

# Who holds a task's connector tools. A fan-out seat holds none: it reads untrusted pages and needs no
# private data. An answer reads a service and never acts in it.
CONNECTORS: Final[Mapping[str, ConnectorReach]] = {
    "lead": "reads",
    "maker": "all",
    "operator": "all",
    "researcher": "all",
    "writer": "all",
}


# The skills a frameless agent may read with `skill_read`, by name: a step's skills are pinned by its
# plan and mounted in its frame, so only an agent without one needs a menu.
SKILLS: Final[Mapping[str, frozenset[str]]] = {
    "lead": frozenset(
        {"compare", "critique", "email", "extract", "markdown", "prismoid-product", "slack", "summarize"}
    ),
}


def held(agent: str | tuple[Grant, ...], met: frozenset[Needs] = EVERYWHERE) -> frozenset[str]:
    """The tools `agent` (a name in `GRANTS`, or a row) holds where the needs in `met` are provided (all of
    them by default)."""
    row = GRANTS.get(agent, ()) if isinstance(agent, str) else agent
    return frozenset(
        g.tool for g in row if (g.needs is None or g.needs in met) and (g.off is None or g.off not in met)
    )


def granted(agent: str | tuple[Grant, ...], met: frozenset[Needs] = DEPLOYED) -> tuple[str, ...]:
    """The record names of the tools `agent` (a name or a row) holds where `met` is provided, in catalog order."""
    names = held(agent, met)
    return tuple(tool.journal for tool in catalog.TOOLS if tool.name in names)


def seat_held(tools: Collection[str]) -> tuple[str, ...]:
    """The record names in `tools` a fan-out seat keeps (`SEAT_TOOLS`)."""
    return tuple(name for name in tools if catalog.model_name(name) in SEAT_TOOLS)


def skill_fits(tools: Collection[str], needs: Collection[str]) -> bool:
    """Whether an agent holding `tools` (record names) holds every tool a skill declares it `needs`."""
    return set(needs) <= {catalog.model_name(name) for name in tools}
