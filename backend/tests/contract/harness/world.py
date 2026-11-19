# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a scenario fakes beside its models: what a model request costs, and the one service it may
be connected to.

`price` puts the real meter in front of the voices of the `billed` agents, at a flat price per
request, so the ledger a budget is read from fills as it does in a live turn. `service` is an MCP
server served in memory in place of the network: the connector's real probe, mount and tool hook
run against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic_ai.models import Model

import prismoid.ai.tools.connectors as connectors
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import EmitUsage, MeterContext, MeteredModel
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit


@dataclass(frozen=True)
class World:
    price: Decimal = Decimal(0)  # USD per model request; zero leaves the voices unmetered
    billed: tuple[str, ...] = ()  # the agents whose requests cost it (the others are free)
    service: FastMCP[Any] | None = None  # the MCP server a connector reaches

    def model(self, voice: Model, agent: str, emit: EmitUsage | None, context: MeterContext) -> Model:
        """The agent's voice as the gateway serves it: behind the real meter when the scenario prices it."""
        if not self.price or agent not in self.billed:
            return voice
        spec = ModelSpec(
            provider="contract",
            model_id="voice",
            family=Family("contract"),
            protocol="function",
            classes=(),
            prices={Unit.REQUEST: self.price},
        )
        return MeteredModel(voice, spec, emit, context)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if self.service is not None:
            served = self.service
            monkeypatch.setattr(connectors, "StreamableHttpTransport", lambda *_, **__: served)


def mail_service() -> FastMCP[Any]:
    """A mail server with one side effect (`send`) and one read (`search`)."""
    mail = FastMCP("mail")

    @mail.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search(query: str) -> str:
        return "nothing found"

    @mail.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def send(to: str, body: str) -> str:
        return f"sent to {to}"

    _ = (search, send)  # registered by the decorator
    return mail
