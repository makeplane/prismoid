# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A page that came back empty says which hosts the sandbox refused while it opened (a bot check
whose solver host is not on the allowlist never reloads the page, and the model would otherwise
tell the person "the page stayed blank" with no cause)."""

from __future__ import annotations

from typing import Any, Final

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools import catalog
from prismoid.ai.tools.browser import BROWSER_TOOLS, browser_toolset
from tests.support.fakes.browser_fakes import FakeSidecar
from tests.support.fakes.browser_fakes import agent as _agent
from tests.support.fakes.browser_fakes import journal as _journal
from tests.support.fakes.browser_fakes import ports as _ports
from tests.support.fakes.browser_fakes import returns as _returns


class _ChallengeShell(FakeSidecar):
    """An AWS WAF challenge whose solver the sandbox refuses while the page opens: an empty shell."""

    def __init__(self, denials: list[dict[str, str]]) -> None:
        super().__init__()
        self.where = {"url": "https://www.amazon.com/", "title": ""}
        self.denials = denials

    async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
        if cmd["verb"] == "open":
            self.denials.append(
                {"ip": "1", "host": "abc.ap-south-1.token.awswaf.com", "reason": "not in allowlist"}
            )
        result = await super().__call__(cmd)  # an open brings its page back too (`snapshot`)
        looks = "text" in result or cmd.get("snapshot")
        return {**result, "epoch": 1, "text": ""} if looks else result


_OPEN: Final = ("browser_open", {"url": "https://www.amazon.com/"})


@pytest.mark.parametrize("calls", [[_OPEN], [_OPEN, ("browser_snapshot", {})]])
async def test_a_blank_page_says_which_hosts_the_sandbox_refused(
    calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """The model saw an empty page with no stated cause and told the person "blank page". The
    refusals are read, never drained (harvest still journals them), and nothing new is journaled."""
    rows: list[tuple[str, bool, str]] = []
    denials: list[dict[str, str]] = []

    async def refused() -> list[dict[str, str]]:
        return list(denials)

    ports = _ports(_ChallengeShell(denials), {"www.amazon.com"}, [])
    ports.denials = refused
    toolset = browser_toolset(BROWSER_TOOLS, _journal(rows), ports, Approvals(tainted=tainted))

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(done := _returns(messages)) < len(calls):
            name, args = calls[len(done)]
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart(str(done[-1].content))])

    out = await _agent(toolset, script).run("look")
    assert "this sandbox refused abc.ap-south-1.token.awswaf.com" in out.output
    assert out.output.rstrip().endswith("only if that could help."), "after the fence, never inside it"
    assert len(denials) == 1
    assert [r[0] for r in rows] == [catalog.journal_name(name) for name, _ in calls]


async def test_a_page_with_content_never_reads_the_refusals() -> None:
    reads: list[int] = []

    async def refused() -> list[dict[str, str]]:
        reads.append(1)
        return [{"ip": "1", "host": "m.media-amazon.com", "reason": "not in allowlist"}]

    ports = _ports(FakeSidecar(), {"admin.example.com"}, [])
    ports.denials = refused
    toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _returns(messages):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_snapshot", args={})])
        return ModelResponse(parts=[TextPart(str(_returns(messages)[0].content))])

    out = await _agent(toolset, script).run("look")
    assert "sandbox refused" not in out.output and reads == []
