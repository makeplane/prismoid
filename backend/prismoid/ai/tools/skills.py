# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`skill_read` - a frameless agent reads a skill's body when the turn needs it.

The Agent Skills pattern: the model sees each skill's name and description up front and reads a body
only when one fits. The menu is values operations froze (sorted by name, each body named by its
content hash), and it rides this tool's description, so it sits in the cached prefix. A name off
the menu is refused as a result, never looked up; the body is read through the `read` port the
mount hands in (`ai/skills/loader`), so this module never reaches the store itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import Field
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools import catalog
from prismoid.ai.tools.call_rows import JournalTool, Said, say

_ROW: Final = catalog.tool("skill_read")
SKILL_READ: Final = _ROW.name
_JOURNAL: Final = _ROW.journal

# A body by its content hash.
ReadBody = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class SkillCard:
    """One skill on a menu: its name, the version the menu froze, its trigger, and its body's hash."""

    name: str
    version: int
    description: str
    body_sha256: str


def menu_of(cards: Iterable[SkillCard]) -> tuple[SkillCard, ...]:
    """`cards` as a frozen menu: one card per name, sorted by name, so the prefix never moves."""
    return tuple(sorted({card.name: card for card in cards}.values(), key=lambda card: card.name))


def _description(cards: tuple[SkillCard, ...]) -> str:
    listed = "\n".join(f"- {card.name}: {card.description}" for card in cards)
    return (
        "Read a skill: packaged guidance for one kind of answer. When a skill's description fits the "
        "turn, read it before you answer and follow it; skip it when none fits. The skills you have:\n"
        f"{listed}"
    )


def skills_toolset(
    cards: Iterable[SkillCard], read: ReadBody, journal: JournalTool | None
) -> FunctionToolset[object] | None:
    """`skill_read` over the menu `cards`; no cards is no tool."""
    menu = menu_of(cards)
    if not menu:
        return None
    by_name = {card.name: card for card in menu}
    toolset: FunctionToolset[object] = FunctionToolset(id="prismoid-skills")

    async def skill_read(name: Annotated[str, Field(description="the skill's name, as listed")]) -> str:
        card = by_name.get(name)
        if card is None:
            reason = "not on this menu"
            await say(journal, Said(_JOURNAL, ok=False, summary=name[:80], denied_reason=reason))
            return f"There is no skill named {name!r}. The skills you have: {', '.join(by_name)}."
        try:
            body = (await read(card.body_sha256)).decode()
        except Exception as failed:  # fail-soft: a person waits on this reply; journalled ok=false
            await say(journal, Said.failed(_JOURNAL, failed, card.name))
            return f"The skill {card.name!r} could not be read just now. Answer without it."
        return f"# Skill: {card.name}\n\n{body.strip()}"

    toolset.add_function(skill_read, name=SKILL_READ, description=_description(menu))
    return toolset
