# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skills for a frameless agent, and pins that honour grants.

A frameless agent sees its granted skills' names and descriptions in `skill_read`'s description (the
cached prefix) and reads a body only when it calls the tool; a skill it is not granted is not on its
menu. A step's pinned skill whose text names a tool its agent does not hold is refused before
anything mounts or inlines it. The Lead's prompt holds no product glossary: it reaches
`prismoid-product` through its menu.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.agents import grants
from prismoid.ai.agents.base import build, get
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.skills.format import parse, parse_dir
from prismoid.ai.skills.loader import LoadedBody, SkillLoader
from prismoid.ai.tools.skills import SKILL_READ, SkillCard
from prismoid.db.models.skill import SkillRecord
from prismoid.operations import skills as skill_ops
from prismoid.operations.skills import BUILTIN_DIR
from prismoid.schemas.contract import Contract, SkillPin
from prismoid.schemas.ids import AgentName, SkillName
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.usage import Unit
from tests.support.skill_menu import builtin_menu

NO_SESSION = cast("AsyncSession", None)  # the registry reads are patched


class _Seen(Exception):
    """The run reached the model request under test; it stops there."""


def _row(name: str) -> ModelSpec:
    return ModelSpec(
        provider="fake",
        model_id="m1",
        family=Family("fake"),
        protocol="openai_compatible",
        classes=(get(name).model_class,),
        base_url="https://example.invalid/v1",
        prices={Unit.INPUT_TOKEN: Decimal(0)},
    )


def _bodies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve the bundled bodies by hash in place of the skill store; the hashes it was asked for."""
    by_sha = {
        card.body_sha256: parse_dir(BUILTIN_DIR / card.name).body.encode() for card in builtin_menu("lead")
    }
    asked: list[str] = []

    def load(_self: SkillLoader, body_sha256: str) -> LoadedBody:
        asked.append(body_sha256)
        return LoadedBody(by_sha[body_sha256], cache_hit=True)

    monkeypatch.setattr(SkillLoader, "load", load)
    return asked


async def _requests(
    name: str, deps: TaskDeps, calls: list[ToolCallPart]
) -> list[tuple[AgentInfo, list[ModelMessage]]]:
    """`name`'s model requests, answering the first with `calls` and stopping at the next."""
    seen: list[tuple[AgentInfo, list[ModelMessage]]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append((info, messages))
        if len(seen) == 1 and calls:
            return ModelResponse(parts=list(calls))
        raise _Seen

    runner = build(name, deps, [_row(name)])
    with runner.agent.override(model=FunctionModel(model)), pytest.raises(_Seen):
        await runner.run("what is a Gate in Prismoid?")
    return seen


def _returned(messages: list[ModelMessage]) -> str:
    parts = [
        p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    assert len(parts) == 1
    return str(parts[0].content)


async def test_a_skill_is_read_only_when_the_model_asks_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _bodies(monkeypatch)
    deps = TaskDeps(skill_menu=builtin_menu("lead"))
    product = parse_dir(BUILTIN_DIR / "prismoid-product").body

    (info, _), *_ = await _requests("lead", deps, [])
    (tool,) = [t for t in info.function_tools if t.name == SKILL_READ]
    assert "- prismoid-product: Load when the user asks what Prismoid or Plane is" in (tool.description or "")
    assert product.splitlines()[0] not in str(info.instructions) + str(tool.description)
    assert asked == []  # the menu costs no read

    call = ToolCallPart(tool_name=SKILL_READ, args={"name": "prismoid-product"}, tool_call_id="c1")
    *_, (_, messages) = await _requests("lead", deps, [call])
    assert _returned(messages) == f"# Skill: prismoid-product\n\n{product.strip()}"
    assert asked == [storage.sha256(product.encode())]


async def test_a_name_off_the_menu_is_refused_and_never_read(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _bodies(monkeypatch)
    call = ToolCallPart(tool_name=SKILL_READ, args={"name": "report"}, tool_call_id="c1")
    *_, (_, messages) = await _requests("lead", TaskDeps(skill_menu=builtin_menu("lead")), [call])
    assert _returned(messages).startswith("There is no skill named 'report'.")
    assert asked == []


async def test_a_failed_body_read_still_lets_the_reply_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    def load(_self: SkillLoader, _body_sha256: str) -> LoadedBody:
        raise OSError("the skill store is down")

    monkeypatch.setattr(SkillLoader, "load", load)
    journaled: list[tuple[str, bool, str]] = []

    async def journal(tool: str, ok: bool, summary: str = "", *_: Any, **__: Any) -> None:
        journaled.append((tool, ok, summary))

    deps = TaskDeps(skill_menu=builtin_menu("lead"), journal_tool=journal)
    call = ToolCallPart(tool_name=SKILL_READ, args={"name": "prismoid-product"}, tool_call_id="c1")
    *_, (_, messages) = await _requests("lead", deps, [call])
    assert _returned(messages).endswith("Answer without it.")
    assert ("skill.read", False, "prismoid-product") in journaled


async def test_no_grant_or_no_menu_is_no_tool() -> None:
    menu = builtin_menu("lead")
    (judge, _), *_ = await _requests("judge", TaskDeps(skill_menu=menu), [])
    (bare, _), *_ = await _requests("lead", TaskDeps(), [])
    assert SKILL_READ not in {t.name for t in (*judge.function_tools, *bare.function_tools)}


def _head(name: str, version: int = 1) -> SkillRecord:
    return SkillRecord(
        name=name, version=version, manifest={"description": f"Load when {name}."}, body_sha256=name * 4
    )


async def test_the_menu_holds_only_granted_skills_sorted_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    heads = [_head("slack", 2), _head("report"), _head("prismoid-product"), _head("markdown")]

    async def published_heads(_session: Any) -> list[SkillRecord]:
        return heads

    monkeypatch.setattr(SkillRecord, "published_heads", published_heads)
    menu = await skill_ops.menu(NO_SESSION, "lead")
    assert [(c.name, c.version) for c in menu] == [("markdown", 1), ("prismoid-product", 1), ("slack", 2)]
    assert await skill_ops.menu(NO_SESSION, "writer") == ()


_MENTIONS = """---
name: careful
description: "Load when the page must be checked first. Not for: drafting."
---
Check the page yourself; do not reach for web_fetch or the `shell` here.
"""
_NEEDS = """---
name: builds
description: "Load when the page must be built. Not for: drafting."
allowed-tools: write_file shell publish
---
Write the page, check it, and hand it over.
"""


@pytest.mark.parametrize(("agent", "kept"), [("writer", ["careful"]), ("maker", ["careful", "builds"])])
async def test_a_pin_is_refused_only_for_a_declared_tool_its_agent_lacks(
    monkeypatch: pytest.MonkeyPatch, agent: str, kept: list[str]
) -> None:
    rows = {
        name: parse(name, text, "", strict=False)
        for name, text in (("careful", _MENTIONS), ("builds", _NEEDS))
    }

    async def at_version(_session: Any, name: str, version: int) -> SkillRecord:
        manifest = rows[name].manifest.model_dump(mode="json")
        return SkillRecord(name=name, version=version, manifest=manifest, body_sha256="0" * 64)

    monkeypatch.setattr(SkillRecord, "at_version", at_version)
    journaled: list[tuple[str, bool, str, str]] = []

    async def journal(
        tool: str, ok: bool, summary: str = "", *_: Any, denied_reason: str = "", **__: Any
    ) -> None:
        journaled.append((tool, ok, summary, denied_reason))

    spec = StepSpec(
        agent=AgentName(agent),
        contract=Contract(goal="Make the page.", acceptance=["it opens"]),
        skill_pins=[SkillPin(name=SkillName(n), version=1) for n in ("careful", "builds")],
    )
    honoured = await skill_ops.honoured(NO_SESSION, spec, get(agent).tools, journal)
    assert [str(pin.name) for pin in honoured.skill_pins] == kept
    refused = (
        []
        if "builds" in kept
        else [
            (
                "skill.read",
                False,
                "builds@1",
                f"{agent} does not hold publish, shell, write_file, which the skill needs",
            )
        ]
    )
    assert journaled == refused


def test_every_granted_skill_is_bundled_and_needs_only_tools_its_reader_holds() -> None:
    for agent, names in grants.SKILLS.items():
        for name in names:
            card: SkillCard = next(c for c in builtin_menu(agent) if c.name == name)
            needs = parse_dir(BUILTIN_DIR / card.name).manifest.allowed_tools
            assert grants.skill_fits(get(agent).tools, needs), (agent, name)


def test_the_lead_holds_no_product_glossary_and_reaches_prismoid_product() -> None:
    prompt = get("lead").prompt
    glossary = (
        "a Gate is",
        "a Routine",
        "a Connector",
        "Capture is",
        "the Inbox",
        "a Skill is",
        "a Project is",
    )
    assert [noun for noun in glossary if noun in prompt] == []
    assert "prismoid-product" in grants.SKILLS["lead"]
    assert "skill.read" in get("lead").tools
