# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What an agent's first model request carries, built through the real `build()` on fake rows and a
fake frame, with every closure a tool family needs. The run stops at that request: the tools it may
call and every text it reads are captured, and nothing is sent anywhere."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents.base import build, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.browser import BrowserPorts
from prismoid.ai.tools.chat import ChatHands
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.skill_menu import builtin_menu


@dataclass(frozen=True)
class Sent:
    """What an agent's first request carries: the tools it may call and every text it reads."""

    tools: frozenset[str]
    texts: dict[str, str]


class _Seen(Exception):
    """The first model request was captured; the run stops there."""


async def _quiet(*_: Any, **__: Any) -> Any:
    return {}


def fake_row(model_class: ModelClass) -> ModelSpec:
    """A free catalog row serving `model_class`, never dialled."""
    return ModelSpec(
        provider="fake",
        model_id=f"m-{model_class.value}",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )


async def first_request(name: str, *, browser: bool, image: bool, chat: bool = False) -> Sent:
    """`name`'s first request, with browser ports when `browser`, an image model when `image` and a
    conversation's card verbs when `chat` (output tools end a run, so `tools` is the function tools alone)."""
    seen: dict[str, AgentInfo] = {}

    def capture(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["info"] = info
        raise _Seen

    deps = TaskDeps(
        frame=FakeFrame("t1"),
        browser=BrowserPorts(browse=_quiet, allow=_quiet) if browser else None,
        propose_routine=_quiet,
        propose_task=_quiet,
        chat=ChatHands(_quiet, _quiet, _quiet) if chat else None,
        skill_menu=builtin_menu(name),
        memory_search=_quiet,
    )
    rows = [fake_row(get(name).model_class), *([fake_row(ModelClass.IMAGE_GEN)] if image else [])]
    runner = build(name, deps, rows)
    with runner.agent.override(model=FunctionModel(capture)), pytest.raises(_Seen):
        await runner.run("go")
    info = seen["info"]
    texts = {"instructions": info.instructions or ""}
    for tool in (*info.function_tools, *info.output_tools):
        texts[f"tool {tool.name}"] = f"{tool.description or ''}\n{tool.parameters_json_schema}"
    return Sent(frozenset(t.name for t in info.function_tools), texts)
