# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Source ids end to end: a read is given its task-wide id, a citing answer names ids only from the task's
reads, and the publish renders each id as its page's link (or keeps the marks for a later citing step)
and refuses one no read was given."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic_ai import Agent, ToolReturn
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.capabilities.citations import CitationCheck
from prismoid.ai.context.citations import UnknownSource, render_citations, sources_brief, unknown_sources
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.research import Shelf, WebRead, citable, sources_read
from prismoid.db.models.plan import PlanStep
from prismoid.errors import ModelIncapable
from prismoid.operations.execution.equip import Equipped
from prismoid.operations.execution.inbox import StepInbox
from prismoid.operations.execution.publish import Output, publish_output
from prismoid.schemas.bots import BotManifest, ResolvedBot
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import ActorId, AgentName, BotName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType, OutputRef, SubmittedDocument
from prismoid.schemas.plan import StepKind, StepSpec

REPORT = "https://example.com/report?q=3"
WIKI = "https://en.wikipedia.org/wiki/Mercury_(planet)"
OTHER = "https://example.org/notes"
SOURCES = {"S1": REPORT, "S2": WIKI}


def _read(call: str, page: str, source: str) -> ModelRequest:
    """A `web_fetch` return as the history keeps it: the read's facts on its metadata, as a dict."""
    meta = WebRead(page=page, source=source, spent=1).model_dump()
    return ModelRequest(parts=[ToolReturnPart("web_fetch", "the page", tool_call_id=call, metadata=meta)])


def test_a_cited_id_becomes_a_numbered_link_and_a_source_list() -> None:
    text = "Revenue rose 14% [S2]. Mercury is small [S1, S2]."
    assert render_citations(text, SOURCES) == (
        f"Revenue rose 14% [[1]](<{WIKI}>). Mercury is small [[2]](<{REPORT}>)[[1]](<{WIKI}>).\n\n"
        f"## Sources\n\n1. <{WIKI}>\n2. <{REPORT}>\n"
    )


def test_a_text_that_cites_nothing_is_left_as_it_is() -> None:
    text = "No citations here, and a link [S1](https://x.example) the model wrote is not one."
    assert render_citations(text, SOURCES) == text


def test_an_unknown_id_is_refused_naming_it_and_the_known_ones() -> None:
    with pytest.raises(UnknownSource) as refused:
        render_citations("A claim [S1]. Another [S9; S1].", SOURCES)
    assert refused.value.ids == ("S9",)
    assert "[S9]" in str(refused.value) and "S1, S2" in str(refused.value)
    assert unknown_sources("[S3] [S9] [S3]", SOURCES) == ["S3", "S9"]


def test_ids_come_from_the_reads_in_the_history() -> None:
    history: list[ModelMessage] = [_read("a", REPORT, "S1"), _read("b", WIKI, "S2"), _read("c", REPORT, "S1")]
    assert sources_read(history) == SOURCES


class _Store:
    """The source store's `cite` side: one id per page for the whole task, the next for a new one."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.cited: list[str] = []

    async def __call__(self, text: str) -> str:
        return "sha"

    async def read(self, sha: str) -> str:
        return "text"

    async def recall(self, source: str) -> str:
        return "text"

    async def cite(self, page: str, sha: str | None) -> str:
        self.cited.append(page)
        return self.ids.setdefault(page, f"S{len(self.ids) + 1}")


async def test_a_shelf_without_a_store_mints_ids_in_read_order_and_a_resumed_one_keeps_them() -> None:
    shelf = Shelf(cites=True)
    assert await shelf.put(REPORT, "text") == "S1" and await shelf.put(REPORT, "text") == "S1"
    assert shelf.shown_id(REPORT) == "S1" and Shelf().shown_id(REPORT) is None
    resumed = Shelf(cites=True)
    await resumed.catch_up([_read("a", WIKI, "S2")], None)
    assert resumed.ids == {WIKI: "S2"}
    assert await resumed.put(REPORT, "text") == "S3", "a new read never takes an id the history gave"


async def test_the_same_page_read_in_two_steps_gets_the_tasks_one_id() -> None:
    store = _Store()
    researcher, writer = Shelf(store, cites=True), Shelf(store, cites=True)
    assert [await researcher.put(p, "t") for p in (WIKI, REPORT)] == ["S1", "S2"]
    assert await writer.put(REPORT, "t") == "S2" and await writer.put(OTHER, "t") == "S3"


async def test_a_replayed_step_keeps_the_ids_its_recorded_reads_carry() -> None:
    store = _Store()
    replayed = Shelf(store, cites=True)
    await replayed.catch_up([_read("a", REPORT, "S3")], None)
    assert await replayed.put(REPORT, "t") == "S3" and store.cited == [], "the history's id, never a new one"


async def test_a_page_the_store_cannot_number_serves_uncitable() -> None:
    class Down(_Store):
        async def cite(self, page: str, sha: str | None) -> str:
            raise OSError("store down")

    shelf = Shelf(Down(), cites=True)
    assert await shelf.put(REPORT, "t") is None and shelf.shown_id(REPORT) is None


def test_a_citing_step_may_name_the_tasks_earlier_ids_and_is_shown_them_without_a_query() -> None:
    assert citable({"S1": WIKI}, [_read("a", REPORT, "S2")]) == {"S1": WIKI, "S2": REPORT}
    brief = sources_brief({"S10": WIKI, "S2": REPORT})
    assert brief.index("[S2] example.com/report") < brief.index("[S10] en.wikipedia.org/wiki/")
    assert "q=3" not in brief and sources_brief({}) == ""


def _citing_agent(answers: list[str]) -> Agent[TaskDeps, str]:
    """A run that reads one page (given S1), then answers with each of `answers` in turn."""

    async def web_fetch(url: str) -> ToolReturn[str]:
        return ToolReturn(f"Source [S1]\n{url}", metadata=WebRead(page=url, source="S1", spent=1))

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("web_fetch", {"url": REPORT})])
        return ModelResponse(parts=[TextPart(answers.pop(0))])

    agent: Agent[TaskDeps, str] = Agent(
        FunctionModel(model), deps_type=TaskDeps, output_type=str, capabilities=[CitationCheck()]
    )
    agent.tool_plain(web_fetch)
    return agent


async def test_an_answer_citing_an_unread_source_is_sent_back_and_fixed() -> None:
    result = await _citing_agent(["It grew [S4].", "It grew [S1]."]).run("go", deps=TaskDeps())
    assert result.output == "It grew [S1]."
    told = [p for m in result.all_messages() if isinstance(m, ModelRequest) for p in m.parts]
    assert any(
        "[S4]" in str(getattr(p, "content", "")) and "S1" in str(getattr(p, "content", "")) for p in told
    )


async def test_an_answer_citing_a_page_an_earlier_step_read_stands() -> None:
    result = await _citing_agent(["It grew [S4]."]).run("go", deps=TaskDeps(sources={"S4": WIKI}))
    assert result.output == "It grew [S4]."


async def test_an_answer_citing_its_reads_stands() -> None:
    result = await _citing_agent(["It grew [S1]."]).run("go", deps=TaskDeps())
    assert result.output == "It grew [S1]."


class _Frame:
    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}
        self.published: list[str] = []

    async def write_bytes(self, path: str, body: bytes) -> None:
        self.written[path] = body

    async def publish(self, path: str, type_: str, title: str, sources: list[str] | None = None) -> None:
        self.published.append(path)


class _Result:
    def __init__(self, output: str | SubmittedDocument, history: list[ModelMessage]) -> None:
        self.output, self._history = output, history

    def all_messages(self) -> list[ModelMessage]:
        return self._history


async def _fact(phase: str, label: str, *, started: float | None = None) -> None:
    return None


def _at(
    frame: _Frame,
    agent: str,
    *,
    sources: dict[str, str] | None = None,
    later: bool = False,
    bots: tuple[ResolvedBot, ...] = (),
) -> Output:
    spec = StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(agent),
        title="find out",
        contract=Contract(
            goal="Find out.",
            acceptance=["answered"],
            output=OutputRef(type=ArtifactType.DOCUMENT, title="Findings"),
        ),
    )
    deps = TaskDeps(sources=sources or {}, bots=bots)
    inbox = StepInbox(uuid.uuid4(), uuid.uuid4(), 1, told=0)
    equipped = Equipped(deps=deps, inputs=(), bots=bots, inbox=inbox, cited_later=later)
    return Output(frame=frame, deps=deps, spec=spec, step=PlanStep(), equipped=equipped, fact=_fact)


async def test_the_researchers_publish_renders_its_cited_pages_as_links() -> None:
    frame = _Frame()
    result = _Result("# Findings\n\nIt grew [S1].", [_read("a", REPORT, "S1")])
    await publish_output(_at(frame, "researcher"), result, override=None)
    (body,) = frame.written.values()
    assert f"It grew [[1]](<{REPORT}>)." in body.decode() and "[S1]" not in body.decode()
    assert frame.published


async def test_the_researchers_publish_refuses_an_unknown_id_and_publishes_nothing() -> None:
    frame = _Frame()
    result = _Result("# Findings\n\nIt grew [S2].", [_read("a", REPORT, "S1")])
    with pytest.raises(ModelIncapable) as refused:
        await publish_output(_at(frame, "researcher"), result, override=None)
    assert refused.value.pickup == "continue" and "[S2]" in str(refused.value)
    assert frame.written == {} and frame.published == []


async def test_findings_a_later_citing_step_reads_keep_their_marks_and_are_still_checked() -> None:
    frame = _Frame()
    result = _Result("# Findings\n\nIt grew [S1].", [_read("a", REPORT, "S1")])
    await publish_output(_at(frame, "researcher", later=True), result, override=None)
    (body,) = frame.written.values()
    assert "It grew [S1]." in body.decode() and "## Sources" not in body.decode()
    with pytest.raises(ModelIncapable):
        bad = _Result("# Findings\n\nIt grew [S2].", [_read("a", REPORT, "S1")])
        await publish_output(_at(_Frame(), "researcher", later=True), bad, override=None)


async def test_the_writer_cites_an_id_an_earlier_step_read_and_its_publish_links_the_page() -> None:
    frame = _Frame()
    memo: Any = _Result(SubmittedDocument(title="Memo", content="Revenue rose 14% [S3]."), [])
    await publish_output(_at(frame, "writer", sources={"S3": REPORT}), memo, override=None)
    (body,) = frame.written.values()
    assert f"Revenue rose 14% [[1]](<{REPORT}>)." in body.decode()
    assert f"## Sources\n\n1. <{REPORT}>" in body.decode()


async def test_the_writer_citing_an_id_the_task_never_gave_publishes_nothing() -> None:
    frame = _Frame()
    memo: Any = _Result(SubmittedDocument(title="Memo", content="See [S9]."), [_read("a", REPORT, "S1")])
    with pytest.raises(ModelIncapable) as refused:
        await publish_output(_at(frame, "writer", sources={"S3": REPORT}), memo, override=None)
    assert "[S9]" in str(refused.value) and "S1, S3" in str(refused.value)
    assert frame.written == {}


async def test_an_agent_that_does_not_cite_publishes_its_text_untouched() -> None:
    frame = _Frame()
    result = _Result("See [S2].", [_read("a", REPORT, "S1")])
    scribe = ResolvedBot(
        name=BotName("scribe"),
        version=1,
        manifest=BotManifest(
            name=BotName("scribe"),
            description="Writes meeting recaps. Load when: a recap is asked. Not for: anything else.",
            capability=ModelClass.FAST_EXECUTOR,
            produces=ArtifactType.DOCUMENT,
            tools=("frame.read_file",),
        ),
        instructions="Write the recap.",
        owner_actor_id=ActorId(uuid.uuid4()),
    )
    await publish_output(_at(frame, "scribe", bots=(scribe,)), result, override=None)
    (body,) = frame.written.values()
    assert "See [S2]." in body.decode()
