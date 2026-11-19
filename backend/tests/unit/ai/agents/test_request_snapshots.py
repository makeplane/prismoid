# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What every agent sends the model on its first request is a reviewed file, not a hash.

Each roster agent is built through the real `build()` with one fixed deps fixture, its first request
captured (the instructions, then every tool and output tool it is sent) and compared with
`tests/snapshots/<agent>/request.txt`. A prompt or tool edit therefore shows up in review as a diff of
the words the model reads. Run ids and the clock are replaced by placeholders before comparing.

The cache-prefix invariant rides along: the instructions and tools are the static prefix every request
of a run re-sends, so they must not move with the clock or with the run, and the per-turn text comes
after them, in the messages. Every role prompt is laid out in named `##` blocks of at most 1,600
characters, and a run records the ids of the blocks it was sent (`prompt_version`).

Re-record after a deliberate change, then review the diff:
`uv run python -m tests.unit.ai.agents.test_request_snapshots`
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import difflib
import json
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai import config
from prismoid.ai.agents.base import build, frame_profile_for, get, instructions_for, prompt_version, roster
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.browser import BrowserPorts
from prismoid.conf import SearchSettings
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.paths import TESTS
from tests.support.skill_menu import builtin_menu

SNAPSHOTS = TESTS / "snapshots"
RECORD = "uv run python -m tests.unit.ai.agents.test_request_snapshots"
BLOCK_CAP = 1600  # characters in one `##` block of a role prompt
TURN = "Summarise the quarterly numbers."  # the per-turn text; it rides the messages, never the prefix

_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")


class _Seen(Exception):
    """The first model request was captured; the run stops there."""


async def _quiet(*_: Any, **__: Any) -> Any:
    return {}


def _deps(name: str, task: uuid.UUID, step: uuid.UUID) -> TaskDeps:
    """The fixed fixture: a fake frame, every callback a tool family needs, browser hands for a browser seat."""
    browser = BrowserPorts(browse=_quiet, allow=_quiet) if frame_profile_for(name) == "browser" else None
    return TaskDeps(
        task_id=TaskId(task),
        step_id=StepId(step),
        frame=FakeFrame(str(task)),
        browser=browser,
        propose_routine=_quiet,
        propose_task=_quiet,
        skill_menu=builtin_menu(name),
        memory_search=_quiet,
    )


def _settings() -> config.Settings:
    """The deployment the snapshots are taken in: one search engine, the computer lane on."""
    search = SearchSettings(engines="searxng", urls="searxng=http://localhost:8888")
    computer_use = config.current().computer_use.model_copy(update={"enabled": True})
    return config.current().model_copy(update={"search": search, "computer_use": computer_use})


async def first_request(name: str, task: uuid.UUID, step: uuid.UUID) -> tuple[str, list[ModelMessage]]:
    """`name`'s first request, unstripped: its static prefix as text, and the messages sent after it."""
    seen: list[tuple[str, list[ModelMessage]]] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = [f"# instructions\n\n{info.instructions or ''}"]
        for kind, tools in (("tool", info.function_tools), ("output tool", info.output_tools)):
            for tool in tools:
                schema = json.dumps(tool.parameters_json_schema, indent=2, sort_keys=True)
                parts.append(f"# {kind}: {tool.name}\n\n{tool.description or ''}\n\n{schema}")
        seen.append(("\n\n".join(parts) + "\n", messages))
        raise _Seen

    with config.use_settings(_settings()):
        runner = build(name, _deps(name, task, step), [_row(name)])
        with runner.agent.override(model=FunctionModel(capture)), pytest.raises(_Seen):
            await runner.run(TURN)
    return seen[0]


def _row(name: str) -> ModelSpec:
    """One fake catalog row serving `name`'s model class."""
    return ModelSpec(
        provider="fake",
        model_id="m1",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(get(name).model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )


def strip(text: str) -> str:
    """The request with run ids and clock readings replaced by placeholders."""
    return _TIME.sub("<time>", _ID.sub("<id>", text))


def volatile(prefix: str, *, ids: tuple[str, ...], now: dt.datetime) -> list[str]:
    """What in a static prefix moves with the run or the clock: a run id, today's date, the per-turn text."""
    marks = [*ids, now.date().isoformat(), now.strftime("%B %-d, %Y"), now.strftime("%-d %B %Y"), TURN]
    return [mark for mark in marks if mark in prefix]


FIXED = (uuid.UUID(int=1), uuid.UUID(int=2))
AGENTS = sorted(str(definition.name) for definition in roster())


def _path(name: str) -> Path:
    return SNAPSHOTS / name / "request.txt"


@pytest.mark.parametrize("name", AGENTS)
async def test_the_first_request_is_the_reviewed_snapshot(name: str) -> None:
    seen = strip((await first_request(name, *FIXED))[0])
    path = _path(name)
    held = path.read_text() if path.exists() else ""
    if seen != held:
        diff = "".join(
            difflib.unified_diff(held.splitlines(True), seen.splitlines(True), str(path), "rendered")
        )
        pytest.fail(
            f"{name}'s first request moved. Review the diff; if it is meant, re-record: {RECORD}\n{diff}"
        )


def test_every_snapshot_is_a_roster_agent() -> None:
    assert sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir()) == AGENTS, f"re-record: {RECORD}"


@pytest.mark.parametrize("name", AGENTS)
async def test_the_static_prefix_holds_no_run_and_no_clock(name: str) -> None:
    """Two runs on different ids send one prefix, it names neither run nor today, and the turn comes after it."""
    other = (uuid.uuid4(), uuid.uuid4())
    prefix, messages = await first_request(name, *other)
    assert prefix == (await first_request(name, *FIXED))[0]
    ids = tuple(str(i) for i in other)
    assert volatile(prefix, ids=ids, now=dt.datetime.now(dt.UTC)) == []
    last = messages[-1]
    assert isinstance(last, ModelRequest)
    assert any(isinstance(p, UserPromptPart) and p.content == TURN for p in last.parts)


def test_a_planted_clock_breaks_the_invariant() -> None:
    """A clock or a run id planted in a seat's static block is caught, wherever in the prefix it sits."""
    now = dt.datetime.now(dt.UTC)
    writer = get("writer")
    assert volatile(instructions_for(writer), ids=("t-1",), now=now) == []
    today = dataclasses.replace(writer, prompt=f"{writer.prompt}\n## Today\n\nIt is {now:%B %-d, %Y}.\n")
    assert volatile(instructions_for(today), ids=("t-1",), now=now) == [now.strftime("%B %-d, %Y")]
    dated = dataclasses.replace(writer, prompt=f"## Today\n\n{now.date().isoformat()}\n\n{writer.prompt}")
    assert volatile(instructions_for(dated), ids=("t-1",), now=now) == [now.date().isoformat()]
    run = dataclasses.replace(writer, prompt=f"{writer.prompt}\nTask t-1.\n")
    assert volatile(instructions_for(run), ids=("t-1",), now=now) == ["t-1"]
    assert strip(f"at {now.isoformat()} on {FIXED[0]}") == "at <time> on <id>"


@pytest.mark.parametrize("name", AGENTS)
def test_the_role_prompt_is_named_blocks(name: str) -> None:
    """Every role prompt opens on a `##` heading; its blocks have distinct ids and fit the cap."""
    blocks = get(name).blocks
    assert blocks[0].id != name, f"{name}'s prompt has text before its first `##` heading"
    ids = [block.id for block in blocks]
    assert len(set(ids)) == len(ids), f"{name} repeats a heading: {ids}"
    assert [b.id for b in blocks if len(b.text) > BLOCK_CAP] == [], f"a block of {name} is over {BLOCK_CAP}"


async def test_a_run_records_the_blocks_it_was_sent() -> None:
    def reply(_: list[ModelMessage], __: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("# Quarterly numbers\n\nRevenue rose.")])

    with config.use_settings(_settings()):
        runner = build("maker", _deps("maker", *FIXED), [_row("maker")])
        with runner.agent.override(model=FunctionModel(reply)):
            result = await runner.run(TURN)
    recorded = (result.metadata or {})["prompt_version"]
    assert recorded == prompt_version(get("maker"))
    assert recorded.split()[-2:] == ["maker.grounding", "maker.your-last-message"]


def record() -> None:
    """Write every agent's snapshot from this tree, and drop the folder of an agent that is gone."""

    async def all_requests() -> dict[str, str]:
        return {name: strip((await first_request(name, *FIXED))[0]) for name in AGENTS}

    for name, text in asyncio.run(all_requests()).items():
        _path(name).parent.mkdir(parents=True, exist_ok=True)
        _path(name).write_text(text)
    for gone in (p for p in SNAPSHOTS.iterdir() if p.is_dir() and p.name not in AGENTS):
        for file in gone.iterdir():
            file.unlink()
        gone.rmdir()


if __name__ == "__main__":
    record()
