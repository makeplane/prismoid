# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every tool an agent is sent is tagged with its side effect in the tool catalog, and every catalog
row is a tool some agent is sent.

Protects grants, steering's "acted outside" and approvals, which all read the tag: an untagged tool
would read as a world act everywhere, or be granted without anyone deciding what it can change.
The population is derived from code: every tool every registered agent, and a Bot's conversation
reply holding every grant a Bot may author, is sent on its first request, with the computer lane off
and on, an image model and a search engine configured, and every closure a tool family needs. A connector's tools are classed from their annotations instead
(`catalog.connector_side_effects`); the framework's own tool search, which only a connector's
deferred tools bring, is tagged without being in that population.
"""

from __future__ import annotations

import uuid

from prismoid.ai import config
from prismoid.ai.agents.base import REGISTRY, roster
from prismoid.ai.agents.bot_compile import compile_bot
from prismoid.ai.tools import catalog
from prismoid.conf import SearchSettings
from prismoid.schemas.bots import BOT_TOOLS, BotManifest, ResolvedBot
from prismoid.schemas.ids import ActorId, BotName
from prismoid.schemas.outputs import ArtifactType
from tests.support.first_request import first_request

_REPLY = compile_bot(
    ResolvedBot(
        name=BotName("every-grant"),
        version=1,
        manifest=BotManifest(
            name=BotName("every-grant"),
            description="Holds every grant a bot may author. Not for routing.",
            use_when=("every grant is needed",),
            tools=tuple(sorted(BOT_TOOLS)),
            produces=ArtifactType.SPREADSHEET,
        ),
        instructions="Reply.",
        owner_actor_id=ActorId(uuid.UUID(int=0)),
    ),
    conversation=True,
)


async def _population() -> dict[str, set[str]]:
    """Every tool the roster is sent, with the agents that hold it."""
    held: dict[str, set[str]] = {}
    search = SearchSettings(engines="searxng", urls="searxng=http://localhost:8888")
    for computer in (False, True):
        computer_use = config.current().computer_use.model_copy(update={"enabled": computer})
        settings = config.current().model_copy(update={"search": search, "computer_use": computer_use})
        with config.use_settings(settings):
            for definition in (*roster(), _REPLY):
                REGISTRY.setdefault(definition.name, definition)  # a reply's compile is no registered agent
                try:
                    sent = await first_request(str(definition.name), browser=True, image=True, chat=True)
                finally:
                    if definition is _REPLY:
                        del REGISTRY[definition.name]
                for tool in sent.tools:
                    held.setdefault(tool, set()).add(str(definition.name))
    return held


def _faults(held: dict[str, set[str]]) -> list[str]:
    """What the law refuses: a tool sent untagged, and a catalog row no agent is sent."""
    tagged = {tool.name for tool in catalog.TOOLS}
    untagged = [
        f"untagged {tool} (on {', '.join(sorted(agents))})"
        for tool, agents in held.items()
        if tool not in tagged
    ]
    unsent = tagged - set(held) - {catalog.FRAMEWORK_TOOL_SEARCH}  # mounted by the framework, not a grant
    return sorted(untagged) + [f"no agent is sent {tool}" for tool in sorted(unsent)]


async def test_every_mounted_tool_is_tagged_and_every_row_mounts() -> None:
    held = await _population()
    assert {"web_search", "browser_click", "generate_image"} <= set(held), "the population lost a family"
    assert not _faults(held), f"tag tools in ai/tools/catalog.py before they mount: {_faults(held)}"


def test_the_law_bites_on_an_untagged_tool() -> None:
    held = {tool.name: {"writer"} for tool in catalog.TOOLS} | {"teleport_user": {"writer"}}
    assert _faults(held) == ["untagged teleport_user (on writer)"]
