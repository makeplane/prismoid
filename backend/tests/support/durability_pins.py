# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log's pinned unit names, derived from the real roster and mounts (ADR-089).

Every registered agent is built with every mount it can take (a frame, a browser, a connector, each
tool port), then each toolset leaf it holds is named the way the backend names its units: the model
request, the event handler, a function tool's call, an MCP server's discovery and call (a
connector's slug varies per task, so it is pinned as `<slug>`), and each capability's logged reads.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec import (
    CapabilityOperationId,
    EventStreamHandlerId,
    JournalOperationNamer,
    ModelRequestId,
    ToolsetCallToolId,
    ToolsetGetToolsId,
)
from pydantic_ai.durable_exec._capability_operation import collect_capability_operations
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import AbstractToolset

from prismoid.ai import config
from prismoid.ai.agents.base import build, roster
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.chat import ChatHands
from prismoid.schemas.connectors import ConnectorSpec
from prismoid.schemas.ids import ConnectorId
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.plan import BudgetFigures
from prismoid.schemas.steering import SafePoint, SteerNote, SteerPending
from prismoid.schemas.usage import Unit
from tests.support.fakes.browser_fakes import FakeSidecar, ports
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.skill_menu import builtin_menu

SLUG = "pinned"


def full_catalog() -> list[ModelSpec]:
    """One fake row serving every model class, so every agent builds."""
    return [
        ModelSpec(
            provider="fake",
            model_id="m1",
            family=Family("fake"),
            protocol="openai_compatible",
            classes=tuple(ModelClass),
            base_url="https://example.invalid/v1",
            prices={Unit.INPUT_TOKEN: Decimal(0)},
        )
    ]


class _Inbox:
    """A steering inbox that never holds a steer: it mounts `Steering`."""

    told = 0
    park_pause = False

    async def pending(self, after_epoch: int) -> SteerPending:
        return SteerPending()

    async def applied(self, steers: Sequence[SteerNote], point: SafePoint) -> None:
        return None

    async def paused_here(self, point: SafePoint) -> None:
        return None


def _every_port() -> TaskDeps:
    async def nothing(*_: Any, **__: Any) -> Any:
        return None

    async def no_cap() -> BudgetFigures | None:
        return None

    connector = ConnectorSpec(
        id=ConnectorId(uuid.uuid4()), slug=SLUG, name="Pinned", url="https://pinned.invalid/mcp"
    )
    return TaskDeps(
        frame=FakeFrame("pins"),
        browser=ports(FakeSidecar(), set(), []),
        connectors=(connector,),
        propose_routine=nothing,
        propose_task=nothing,
        chat=ChatHands(nothing, nothing, nothing),
        steering=_Inbox(),
        spend=no_cap,
        skill_menu=builtin_menu("lead"),
        memory_search=nothing,
    )


def operation_names() -> list[str]:
    """Every unit name a built agent can log, sorted; with a search engine set, whatever the host has."""
    settings = config.current()
    search = settings.search.model_copy(
        update={"engines": "searxng", "urls": "searxng=https://search.invalid"}
    )
    with config.use_settings(settings.model_copy(update={"search": search})):
        return _names()


def _names() -> list[str]:
    names: set[str] = set()
    for definition in roster():
        names.update(_agent_names(str(definition.name), _every_port()))
        if definition.browses:  # its other seat: a step off the browser holds the connect block
            names.update(_agent_names(str(definition.name), dataclasses.replace(_every_port(), browser=None)))
    return sorted(names)


def _agent_names(agent: str, deps: TaskDeps) -> set[str]:
    """Every unit name one build of `agent` with `deps` can log."""
    names: set[str] = set()
    runner = build(agent, deps, full_catalog())
    namer = JournalOperationNamer(agent)
    names.add(namer.operation_name(ModelRequestId(model_name="default")))
    names.add(namer.operation_name(ModelRequestId(streaming=True, model_name="default")))
    names.add(namer.operation_name(EventStreamHandlerId()))
    leaves: list[AbstractToolset[Any]] = []
    for toolset in runner.agent.toolsets:
        toolset.apply(leaves.append)
    for leaf in leaves:
        names.update(_leaf_names(namer, leaf))
    capabilities: list[AbstractCapability[Any]] = []
    runner.agent.root_capability.apply(capabilities.append)
    for capability in capabilities:
        for operation in collect_capability_operations(capability):
            assert capability.id is not None, f"{type(capability).__name__} logs a read with no pinned id"
            names.add(namer.operation_name(CapabilityOperationId(capability.id, operation=operation)))
    return names


def _leaf_names(namer: JournalOperationNamer, leaf: AbstractToolset[Any]) -> set[str]:
    if isinstance(leaf, FunctionToolset) and leaf.id is not None:
        call = ToolsetCallToolId("function", toolset_id=leaf.id)
        return {namer.invocation_name(call, label=tool).operation_name for tool in leaf.tools}
    if isinstance(leaf, MCPToolset) and leaf.id is not None:
        found = {
            namer.operation_name(ToolsetGetToolsId("mcp", toolset_id=leaf.id)),
            namer.operation_name(ToolsetCallToolId("mcp", toolset_id=leaf.id)),
        }
        return {name.replace(f"__{leaf.id}.", "__<slug>.") for name in found}
    return {f"unnamed leaf {type(leaf).__name__}"}
