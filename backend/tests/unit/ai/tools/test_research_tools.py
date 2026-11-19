# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`find_in_page`, `note` and the page `web_fetch` keeps.

`web_fetch` runs over a mock transport and a table for DNS; the shelf it fills is the one
`find_in_page` and `note` read, as the mount wires them. A kept page's sha rides the call's row, and
a run resumed from its call log (`tests/support/rigs/durability_drill.py`) still holds what it read.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools import labels
from prismoid.ai.tools.fetch import FetchCaps, fetch_toolset
from prismoid.ai.tools.research import KeepPage, Shelf, research_toolset
from prismoid.schemas.ids import StepId
from tests.support.fetched import on_the_wire, shown
from tests.support.rigs.durability_drill import Killed, MemoryLog, World
from tests.support.runners import PLAIN

URL = "https://example.com/report"
LINK = "https://example.com/appendix"  # only the report links to it
PAGE = (
    b"<html><head><title>Q3</title></head><body><article><h1>Q3 report</h1><p>"
    + b"Revenue grew 14.2 percent to 31 million euros in the third quarter. " * 3
    + b"</p><p>The board said margins would\n stay   flat next year.</p>"
    + b'<p>The detail is in the <a href="/appendix">appendix</a>.</p></article></body></html>'
)
APPENDIX = (
    b"<html><head><title>Appendix</title></head><body><article><h1>Appendix</h1><p>"
    + b"Costs fell 3 percent across every region in the quarter. " * 3
    + b"</p></article></body></html>"
)


@dataclass
class Journal:
    rows: list[tuple[str, bool, str, str | None]] = field(
        default_factory=list[tuple[str, bool, str, str | None]]
    )

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        self.rows.append((tool, ok, summary, ref))


@dataclass
class Keeper:
    kept: dict[str, str] = field(default_factory=dict[str, str])
    ids: dict[str, tuple[str, str | None]] = field(default_factory=dict[str, tuple[str, str | None]])

    async def __call__(self, text: str) -> str:
        sha = hashlib.sha256(text.encode()).hexdigest()
        self.kept[sha] = text
        return sha

    async def read(self, sha: str) -> str:
        return self.kept[sha]

    async def cite(self, page: str, sha: str | None) -> str:
        given = next((sid for sid, (was, _) in self.ids.items() if was == page), f"S{len(self.ids) + 1}")
        self.ids.setdefault(given, (page, sha))
        return given

    async def recall(self, source: str) -> str:
        sha = self.ids[source][1]
        if sha is None:
            raise LookupError(source)
        return self.kept[sha]


class Ctx:
    def __init__(self) -> None:
        self.tool_call_id = "t1"
        self.messages = [ModelRequest(parts=[UserPromptPart(f"read {URL}")])]


async def _dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _caps() -> FetchCaps:
    return FetchCaps(5.0, 1_000_000, 3, 5, 12_000, 60.0, True, False)


def _site(asked: list[str]) -> httpx.MockTransport:
    def serve(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        page = APPENDIX if request.url.path == "/appendix" else PAGE
        return httpx.Response(200, content=page, headers={"content-type": "text/html"})

    return httpx.MockTransport(on_the_wire(serve))


def _toolsets(
    journal: Journal | None, keeper: KeepPage | None, asked: list[str], ids: dict[str, str] | None = None
) -> list[FunctionToolset[Any]]:
    """The web door and the research tools over one shelf, as the mount wires them (`ids`: the task's earlier
    pages to their source ids)."""
    shelf = Shelf(keeper, ids=dict(ids or {}))
    door = fetch_toolset(
        ("web.fetch",),
        journal,
        client_factory=lambda: httpx.AsyncClient(transport=_site(asked)),
        resolver=_dns,
        caps=_caps(),
        shelf=shelf,
    )
    research = research_toolset(("research.find_in_page", "research.note"), shelf, journal)
    assert door is not None and research is not None
    return [door, research]


def _hands(
    journal: Journal,
    keeper: KeepPage | None,
    asked: list[str] | None = None,
    ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    toolsets = _toolsets(journal, keeper, [] if asked is None else asked, ids)
    hands = {name: tool.function for ts in toolsets for name, tool in ts.tools.items()}
    return {name: shown(hand) for name, hand in hands.items()}


async def test_a_read_page_is_kept_and_its_sha_rides_the_row() -> None:
    journal, keeper = Journal(), Keeper()
    hands = _hands(journal, keeper)
    await hands["web_fetch"](Ctx(), URL)
    [(sha, text)] = keeper.kept.items()
    assert "31 million euros" in text
    assert journal.rows == [("web.fetch", True, "example.com/report", sha)], (
        "the sha is the ref, never the label"
    )
    assert labels.page_read("web.fetch", journal.rows[0][2]) == "example.com/report"


async def test_a_page_with_no_text_is_never_kept_or_given_an_id() -> None:
    journal, keeper = Journal(), Keeper()
    blank = httpx.MockTransport(
        on_the_wire(lambda _r: httpx.Response(200, content=b"", headers={"content-type": "text/plain"}))
    )
    shelf = Shelf(keeper)
    door = fetch_toolset(
        ("web.fetch",),
        journal,
        client_factory=lambda: httpx.AsyncClient(transport=blank),
        resolver=_dns,
        caps=_caps(),
        shelf=shelf,
    )
    assert door is not None
    await shown(door.tools["web_fetch"].function)(Ctx(), URL)
    assert keeper.kept == {} and keeper.ids == {}, "an empty read is no source: nothing to cite or mount"


async def test_a_store_that_fails_still_serves_the_page() -> None:
    class Broken:
        async def __call__(self, text: str) -> str:
            raise OSError("store down")

        async def read(self, sha: str) -> str:
            raise OSError("store down")

        async def cite(self, page: str, sha: str | None) -> str:
            raise OSError("store down")

        async def recall(self, source: str) -> str:
            raise OSError("store down")

    journal = Journal()
    hands = _hands(journal, Broken())
    shown = await hands["web_fetch"](Ctx(), URL)
    assert "31 million euros" in shown
    assert journal.rows == []  # the recorder writes the plain row; no sha is claimed


async def test_find_in_page_finds_exact_text_across_case_and_line_breaks() -> None:
    hands = _hands(Journal(), Keeper())
    await hands["web_fetch"](Ctx(), URL)
    found = await hands["find_in_page"](Ctx(), URL, "MARGINS   would\nstay flat")
    assert found.startswith("1 match(es) on https://example.com/report")
    assert "[at " in found and "margins would stay flat" in found
    missing = await hands["find_in_page"](Ctx(), URL, "margins will stay flat")
    assert "is not on" in missing


async def test_find_in_page_refuses_a_page_the_run_never_read() -> None:
    journal = Journal()
    hands = _hands(journal, Keeper())
    refused = await hands["find_in_page"](Ctx(), "https://example.org/other", "revenue")
    assert "not read in this run" in refused
    assert journal.rows[-1][:2] == ("research.find_in_page", False)


async def test_note_keeps_findings_on_read_pages_only() -> None:
    hands = _hands(Journal(), Keeper())
    await hands["web_fetch"](Ctx(), URL)
    kept = await hands["note"](Ctx(), "Revenue +14.2% in Q3", [URL])
    assert kept == f"Notes so far:\n1. Revenue +14.2% in Q3 [{URL}]"
    refused = await hands["note"](Ctx(), "a guess", ["https://example.org/never"])
    assert "not read in this run" in refused
    again = await hands["note"](Ctx(), "margins flat next year")
    assert again.endswith("2. margins flat next year")


async def test_a_writer_checks_the_page_a_researcher_read_with_no_second_fetch() -> None:
    """A later step's shelf holds the task's earlier ids, not their text: the first ask for one reads the
    page back from the store, the same for a re-run of that step, and an id the task never gave stays refused."""
    keeper, asked = Keeper(), []
    await _hands(Journal(), keeper, asked)["web_fetch"](Ctx(), URL)
    ids = {page: sid for sid, (page, _) in keeper.ids.items()}
    assert ids == {URL: "S1"}

    found = [
        await _hands(Journal(), keeper, asked, ids)["find_in_page"](Ctx(), "S1", "margins would stay flat")
        for _ in range(2)  # the writer's step, and that step run again
    ]
    assert found[0].startswith(f"1 match(es) on {URL}") and found[1] == found[0], found
    writer = _hands(Journal(), keeper, asked, ids)
    assert (await writer["note"](Ctx(), "margins flat", ["S1"])).endswith(f"1. margins flat [{URL}]")
    assert "not read in this run" in await writer["find_in_page"](Ctx(), "S9", "margins")
    assert asked == ["/report"], "the researcher's one read, never a second"


def test_research_mounts_only_what_is_granted() -> None:
    assert research_toolset((), Shelf(), None) is None
    only = research_toolset(("research.note",), Shelf(), None)
    assert only is not None and set(only.tools) == {"note"}


def test_a_fetch_label_is_the_page_it_read() -> None:
    assert labels.page_read("web.fetch", "go.dev/doc/devel/release") == "go.dev/doc/devel/release"


async def test_a_resumed_run_still_holds_the_pages_links_and_notes_it_read() -> None:
    """A replayed call returns its recorded result without running, so the shelf is rebuilt from
    those returns: after a death, the page read before it is still findable and citable, and a link
    found on it still followable, with nothing fetched twice."""
    step, log, keeper, asked = StepId(uuid.uuid4()), MemoryLog(), Keeper(), []
    world = World(
        [
            [("web_fetch", {"url": URL})],
            [("note", {"text": "Revenue +14.2% in Q3", "sources": [URL]})],
            [
                ("find_in_page", {"url": URL, "text": "margins would stay flat"}),
                ("note", {"text": "margins flat next year", "sources": [URL]}),
                ("web_fetch", {"url": LINK}),
            ],
        ],
        die_before=3,
    )

    async def attempt() -> Any:
        agent = Agent(
            world.model(),
            name="reader",
            deps_type=TaskDeps,
            toolsets=_toolsets(None, keeper, asked),
            output_type=[str, DeferredToolRequests],
            capabilities=[PrismoidDurability()],
        )
        scope = await CallLogScope.open(log, step, fresh=False)
        return await AgentRunner(agent, TaskDeps(step_id=step), PLAIN).run(f"read {URL}", log=scope)

    with pytest.raises(Killed):
        await attempt()
    world.die_before = None
    result = await attempt()

    last = [m for m in result.all_messages() if isinstance(m, ModelRequest)][-1]
    said = {p.tool_name: p.model_response_str() for p in last.parts if isinstance(p, ToolReturnPart)}
    assert said["find_in_page"].startswith(f"1 match(es) on {URL}"), said["find_in_page"]
    assert said["note"].endswith(f"2. margins flat next year [{URL}]"), said["note"]
    assert "Costs fell 3 percent" in said["web_fetch"], said["web_fetch"]
    assert asked == ["/report", "/appendix"], "nothing fetched twice"


async def test_a_page_with_many_links_carries_only_the_first_ones_and_says_so() -> None:
    """A huge page cannot bloat the history: its first 200 links are followable, the rest are counted."""
    links = "".join(f'<a href="/p{n}">p{n}</a> ' for n in range(250))
    hub = f"<html><body><article><p>{'An index of pages. ' * 20}</p><p>{links}</p></article></body></html>"

    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=hub.encode(), headers={"content-type": "text/html"})

    door = fetch_toolset(
        ("web.fetch",),
        None,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(on_the_wire(serve))),
        resolver=_dns,
        caps=_caps(),
    )
    assert door is not None
    fetch: Any = door.tools["web_fetch"].function
    read = await fetch(Ctx(), URL)

    assert read.metadata.links == tuple(f"https://example.com/p{n}" for n in range(200))
    assert "links to 50 more pages than can be followed" in read.return_value
    assert "could not serve" not in await shown(fetch)(Ctx(), "https://example.com/p199")
    refused = await shown(fetch)(Ctx(), "https://example.com/p200")
    assert "not mentioned anywhere in this task" in refused or "that exact link is not" in refused, refused
