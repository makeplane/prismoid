# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plain words where people and models read: no document references in `--help`, in prompts, or in
the tool and parameter descriptions a model is sent.

Protects the reader of `prismoid --help` and the model reading its prompt. A bare `§10`, an `ADR-029`,
a milestone tag like `M5.2` or a `Rule 5` points into documents neither of them has, so it costs a
person attention and a model tokens and tells either nothing; the sentence around it has to carry
the meaning on its own. Comments and module docstrings are not covered: a contributor can follow
the reference.

The population is derived from code: every command, group and parameter of the `prismoid` Typer app
(its help text, so a command's docstring too), every `.md` file under `ai/agents/prompts/`, and every
tool every registered agent is sent on its first request, with the computer lane off and on (a
connector's tools are its server's words, not ours).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, cast

import pytest
import typer
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition

from prismoid.ai import config
from prismoid.ai.agents.base import build, get, roster
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.browser import BrowserPorts
from prismoid.ai.tools.chat import ChatHands
from prismoid.conf import SearchSettings
from prismoid.runner.main import app
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.paths import BACKEND

PROMPTS = BACKEND / "prismoid" / "ai" / "agents" / "prompts"
REFERENCE = re.compile(r"§\s*\d+|\bADR-\d+|\bM\d+\.\d+\b|\bRule \d+\b")


def references(text: str) -> list[str]:
    """Every document reference in `text`."""
    return [m.group(0) for m in REFERENCE.finditer(text)]


def _help_texts(command: Any, path: str) -> Iterator[tuple[str, str]]:
    yield path, command.help or ""
    for param in command.params:
        yield f"{path} {'/'.join(param.opts)}", getattr(param, "help", None) or ""
    if hasattr(command, "list_commands"):
        ctx = command.context_class(command)
        for name in command.list_commands(ctx):
            yield from _help_texts(command.get_command(ctx, name), f"{path} {name}")


def test_cli_help_speaks_plain_words() -> None:
    found = {
        where: refs
        for where, text in _help_texts(typer.main.get_command(app), "prismoid")
        if (refs := references(text))
    }
    assert not found, f"say it in words, without the reference: {found}"


def test_prompts_speak_plain_words() -> None:
    prompts = sorted(PROMPTS.glob("*.md"))
    assert prompts, f"no prompt files under {PROMPTS}"
    found = {p.name: refs for p in prompts if (refs := references(p.read_text(encoding="utf-8")))}
    assert not found, f"say it in words, without the reference: {found}"


class _Seen(Exception):
    """The first model request was captured; the run stops there."""


async def _quiet(*_: Any, **__: Any) -> Any:
    return {}


def _descriptions(schema: object, where: str) -> Iterator[tuple[str, str]]:
    """Every `description` in a JSON schema, parameters and nested models alike."""
    if isinstance(schema, dict):
        for key, value in cast("dict[str, object]", schema).items():
            if key == "description" and isinstance(value, str):
                yield where, value
            else:
                yield from _descriptions(value, f"{where}.{key}")
    elif isinstance(schema, list):
        for item in cast("list[object]", schema):
            yield from _descriptions(item, where)


async def _sent_tools(name: str) -> list[ToolDefinition]:
    """The tools `name` is sent on its first request, with every closure a tool family needs."""
    seen: list[ToolDefinition] = []

    def capture(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend([*info.function_tools, *info.output_tools])
        raise _Seen

    deps = TaskDeps(
        frame=FakeFrame("t1"),
        browser=BrowserPorts(browse=_quiet, allow=_quiet),
        propose_routine=_quiet,
        propose_task=_quiet,
        chat=ChatHands(_quiet, _quiet, _quiet),
    )
    row = ModelSpec(
        provider="fake",
        model_id="m1",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(get(name).model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )
    runner = build(name, deps, [row])
    with runner.agent.override(model=FunctionModel(capture)), pytest.raises(_Seen):
        await runner.run("go")
    return seen


async def tool_texts(*, computer: bool) -> dict[str, str]:
    """Every tool and parameter description the roster is sent, keyed by agent, tool and path."""
    search = SearchSettings(engines="searxng", urls="searxng=http://localhost:8888")
    computer_use = config.current().computer_use.model_copy(update={"enabled": computer})
    settings = config.current().model_copy(update={"search": search, "computer_use": computer_use})
    texts: dict[str, str] = {}
    with config.use_settings(settings):
        for name in (str(definition.name) for definition in roster()):
            for tool in await _sent_tools(name):
                texts[f"{name}:{tool.name}"] = tool.description or ""
                for where, text in _descriptions(tool.parameters_json_schema, f"{name}:{tool.name}"):
                    texts[where] = text
    return texts


@pytest.mark.parametrize("computer", [False, True])
async def test_tools_speak_plain_words(computer: bool) -> None:
    texts = await tool_texts(computer=computer)
    assert any(key.endswith(":web_search") for key in texts), "the population lost a tool family"
    assert any(key.endswith(":browser_click") for key in texts), "the population lost a tool family"
    found = {where: refs for where, text in texts.items() if (refs := references(text))}
    assert not found, f"say it in words, without the reference: {found}"


def test_the_law_bites() -> None:
    assert references("traces every claim back to a source (§10)") == ["§10"]
    assert references("FIT the assigned size (ADR-029).") == ["ADR-029"]
    assert references("loads into the frame (M5.2), and") == ["M5.2"]
    assert references("a model call (Rule 5)") == ["Rule 5"]
    assert references("subsection 7.2 of the outline, an M3 milestone, MADR-1, rule five") == []
    nested = {"properties": {"lane": {"description": "see Rule 9"}, "rows": {"items": {"description": "x"}}}}
    assert [text for _, text in _descriptions(nested, "tool")] == ["see Rule 9", "x"]
