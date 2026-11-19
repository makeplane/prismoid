# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tool catalog: every built-in tool once, with the name the record holds it by, what it can
change (`SideEffect`) and whose text its result is (`Taint`). The one tool-name table: every other
spelling of a tool's name derives from it, a tool missing here cannot mount (`tests/laws/test_law_tool_catalog.py`), and `TOOLS` is in catalog order.

A connector's tools are open vocabulary, so they are classed when read rather than listed: a tool
its server declared read-only (and not destructive) is a read, anything else acts in the world, and
its result is always a third party's text.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from pydantic_ai.capabilities import ToolSearch

from prismoid.schemas.connectors import ConnectorToolInfo
from prismoid.schemas.egress import Taint
from prismoid.schemas.side_effects import SideEffect

# A tool the framework mounts itself, never a grant: the local fallback of its tool search.
FRAMEWORK_TOOL_SEARCH: Final = ToolSearch.function_tool_name


@dataclass(frozen=True)
class CatalogTool:
    """One tool: the name the model calls, the name the record holds, what a call can change, whose
    text its result is, and whether a call mints rows that outlive the task (two calls, two rows)."""

    name: str
    journal: str
    side_effect: SideEffect
    taint: Taint = Taint.TRUSTED
    mints: bool = False


TOOLS: Final[tuple[CatalogTool, ...]] = (
    CatalogTool("read_file", "frame.read_file", SideEffect.READ, Taint.USER),
    CatalogTool("write_file", "frame.write_file", SideEffect.LOCAL),
    CatalogTool("edit", "frame.edit", SideEffect.LOCAL, Taint.USER),
    CatalogTool("shell", "frame.shell", SideEffect.LOCAL, Taint.USER),
    CatalogTool("publish", "frame.publish", SideEffect.RECORD),
    CatalogTool("ask_user", "ask_user", SideEffect.DEFERRAL, Taint.USER),
    CatalogTool("request_connector", "request_connector", SideEffect.DEFERRAL),
    CatalogTool("web_fetch", "web.fetch", SideEffect.READ, Taint.THIRD_PARTY),
    CatalogTool("web_search", "web.search", SideEffect.READ, Taint.THIRD_PARTY),
    # Over the pages the run already read and the notes it keeps: nothing outside the run.
    CatalogTool("find_in_page", "research.find_in_page", SideEffect.PURE, Taint.THIRD_PARTY),
    CatalogTool("note", "research.note", SideEffect.PURE),
    CatalogTool("generate_image", "media.generate_image", SideEffect.LOCAL),
    CatalogTool("routine_create", "routine.create", SideEffect.RECORD, mints=True),
    CatalogTool("propose_task", "propose_task", SideEffect.RECORD),
    # A conversation's card verbs: each records what the newest line asks of a card; the reply lane acts.
    CatalogTool("continue_card", "chat.continue_card", SideEffect.RECORD),
    CatalogTool("accept_pending", "chat.accept_pending", SideEffect.RECORD),
    CatalogTool("decline_pending", "chat.decline_pending", SideEffect.RECORD),
    # A frozen skill body by its content hash: the same bytes on every replay.
    CatalogTool("skill_read", "skill.read", SideEffect.PURE),
    # The person's own remembered notes: a read of their ledger, in their words.
    CatalogTool("memory_search", "memory.search", SideEffect.READ, Taint.USER),
    # The browser's verbs drive one live session the world shares (its pages, cookies and forms),
    # so a look acts as surely as a click until its results say what changed.
    CatalogTool("browser_open", "browser.open", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_back", "browser.back", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_wait", "browser.wait", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_snapshot", "browser.snapshot", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_click", "browser.click", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_type", "browser.type", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_fill", "browser.fill", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_select", "browser.select", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_press", "browser.press", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_scroll", "browser.scroll", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_screenshot", "browser.screenshot", SideEffect.WORLD, Taint.THIRD_PARTY),
    CatalogTool("browser_handoff", "browser.handoff", SideEffect.DEFERRAL),
    # pydantic-ai's own tool search: it finds a connector's deferred tools and runs none of them.
    CatalogTool(FRAMEWORK_TOOL_SEARCH, FRAMEWORK_TOOL_SEARCH, SideEffect.PURE),
)

_BY_NAME: Final = {tool.name: tool for tool in TOOLS}
_BY_JOURNAL: Final = {tool.journal: tool for tool in TOOLS}


def tool(name: str) -> CatalogTool:
    """The row of a tool the model calls `name`; KeyError when the catalog does not hold it."""
    return _BY_NAME[name]


def known(*names: str) -> frozenset[str]:
    """Model-facing names checked against the catalog, refusing one it does not hold."""
    if unknown := [name for name in names if name not in _BY_NAME]:
        raise KeyError(f"not in the tool catalog: {unknown}")
    return frozenset(names)


def family(namespace: str) -> tuple[CatalogTool, ...]:
    """The tools journaled under `namespace` (`browser` holds `browser.open`, ...), in catalog order."""
    return tuple(tool for tool in TOOLS if tool.journal.startswith(f"{namespace}."))


def journal_name(name: str) -> str:
    """The name the record holds a model-facing tool by; a name the catalog does not hold (a
    connector's) is the same in both."""
    row = _BY_NAME.get(name)
    return row.journal if row is not None else name


def model_name(journal: str) -> str:
    """The name the model calls a journaled tool by; a name the catalog does not hold (a
    connector's) is the same in both."""
    row = _BY_JOURNAL.get(journal)
    return row.name if row is not None else journal


def side_effect_of(journal: str) -> SideEffect | None:
    """The class of a call by its journal name; None when the catalog does not hold it."""
    row = _BY_JOURNAL.get(journal)
    return row.side_effect if row is not None else None


def mints(journal: str) -> bool:
    """Does a call, by its journal name, mint rows outside the task (a routine, its knock)?"""
    row = _BY_JOURNAL.get(journal)
    return row is not None and row.mints


def taint_of(name: str) -> Taint:
    """Whose text a call's result is; a name the catalog does not hold (a connector's tool) is a
    third party's, so an unlisted tool fails closed."""
    row = _BY_NAME.get(name)
    return row.taint if row is not None else Taint.THIRD_PARTY


def connector_tool(slug: str, name: str) -> str:
    """A connector tool's name as it mounts and journals: the server's own name under the slug."""
    return f"{slug}_{name}"


def connector_side_effects(slug: str, tools: Iterable[ConnectorToolInfo]) -> dict[str, SideEffect]:
    """A connector's tools by the name they mount and journal under (`connector_tool`): a read only
    when its server declared it read-only and not destructive, otherwise a world act."""
    return {
        connector_tool(slug, t.name): SideEffect.READ
        if t.read_only is True and t.destructive is not True
        else SideEffect.WORLD
        for t in tools
    }
