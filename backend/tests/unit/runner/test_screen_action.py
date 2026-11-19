# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`screen.action`: after each acting verb that succeeds, the browser toolset hands the
verb, the page's URL and the element's box to `BrowserPorts.on_action`; the engine publishes it as
a Lane-2 `screen.action` with host and path only - never a query string, which can carry a token."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, BrowserPorts, browser_toolset
from prismoid.operations import live
from prismoid.operations.execution import ports as ex_ports
from prismoid.runner.stream import _render  # pyright: ignore[reportPrivateUsage]
from prismoid.schemas import events as ev
from prismoid.schemas.ids import ProjectId, StepId, TaskId

BOX = {"x": 412.0, "y": 230.5, "w": 96.0, "h": 32.0}
URL = "https://admin.example.com/users?token=s3cret#row-7"


class Sidecar:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(cmd)
        verb = cmd["verb"]
        if verb == "inspect":
            return {
                "ok": True,
                "role": "button",
                "name": "Save",
                "host": "admin.example.com",
                "path": "/users",
            }
        if not self.ok:
            return {"ok": False, "error": "stale reference e1.1; take a new snapshot"}
        out: dict[str, Any] = {"ok": True, "url": URL, "title": "Users"}
        if verb in ("click", "type", "fill", "select"):
            out["box"] = dict(BOX)
        if verb == "snapshot":
            out["text"] = '[e1.1] button "Save"'
        return out


def _ports(sidecar: Sidecar, seen: list[dict[str, Any]], *, broken: bool = False) -> BrowserPorts:
    async def allow(domains: list[str]) -> None:
        return None

    async def on_action(action: Mapping[str, Any]) -> None:
        seen.append(dict(action))
        if broken:
            raise RuntimeError("the notifier is down")

    return BrowserPorts(browse=sidecar, allow=allow, allowed={"admin.example.com"}, on_action=on_action)


def _agent(toolset: FunctionToolset[Any] | None, tool: str, args: dict[str, Any]) -> Agent[None, Any]:
    assert toolset is not None

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    return Agent(FunctionModel(script), output_type=[str, DeferredToolRequests], toolsets=[toolset])


async def test_an_ok_acting_verb_reports_its_verb_url_and_box() -> None:
    seen: list[dict[str, Any]] = []
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(Sidecar(), seen), Approvals(tainted=tainted))
    out = await _agent(toolset, "browser_click", {"ref": "e1.1"}).run("go")
    assert str(out.output).startswith("ok ·")
    assert seen == [{"verb": "click", "url": URL, "box": BOX}]


class _Shots(Sidecar):
    """A screenshot's answer: a picture, not an act."""

    async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(cmd)
        return {"ok": True, "png_b64": "iVBORw0KGgo=", "url": URL, "title": "Users"}


@pytest.mark.parametrize(
    ("sidecar", "tool", "args"),
    [
        (Sidecar(), "browser_snapshot", {}),
        (_Shots(), "browser_screenshot", {}),
        (Sidecar(ok=False), "browser_click", {"ref": "e1.1"}),
    ],
    ids=["snapshot", "screenshot", "failed-click"],
)
async def test_a_read_or_a_failed_act_reports_nothing(
    sidecar: Sidecar, tool: str, args: dict[str, Any]
) -> None:
    seen: list[dict[str, Any]] = []
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, seen), Approvals(tainted=tainted))
    await _agent(toolset, tool, args).run("go")
    assert seen == []


async def test_a_broken_publisher_never_breaks_the_call() -> None:
    seen: list[dict[str, Any]] = []
    toolset = browser_toolset(
        BROWSER_TOOLS, None, _ports(Sidecar(), seen, broken=True), Approvals(tainted=tainted)
    )
    out = await _agent(toolset, "browser_open", {"url": "https://admin.example.com/users"}).run("go")
    assert str(out.output).startswith("ok ·") and seen == [{"verb": "open", "url": URL}]


async def test_the_engine_publishes_host_and_path_never_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    async def notify(event: Any) -> None:
        sent.append(event)

    monkeypatch.setattr(live, "notify", notify)
    task, step, project = TaskId(uuid.uuid4()), StepId(uuid.uuid4()), ProjectId(uuid.uuid4())
    publish = ex_ports.screen_actions(task, step, project)
    await publish({"verb": "click", "url": URL, "box": BOX})
    await publish({"verb": "back", "url": "https://admin.example.com/"})
    await publish({"verb": "type", "url": "http://[::1", "box": {"x": "left"}})
    first, second, third = sent
    assert isinstance(first, ev.ScreenAction)
    assert (first.verb, first.host, first.path) == ("click", "admin.example.com", "/users")
    assert first.box == ev.ScreenBox(**BOX)
    wire = first.model_dump_json()
    assert "s3cret" not in wire and "token" not in wire and "row-7" not in wire
    assert (first.task_id, first.step_id, first.project_id, first.lane) == (task, step, project, "live")
    assert second.box is None and second.path == "/"
    assert third.box is None and third.host == "" and third.path == "", "a malformed answer is dropped"


def test_the_flight_recorder_names_the_act() -> None:
    line = _render(
        {
            "type": "screen.action",
            "lane": "live",
            "verb": "click",
            "host": "admin.example.com",
            "path": "/users",
        }
    )
    assert line.startswith("[  -] screen.action") and line.endswith("click admin.example.com/users")


async def test_only_a_frame_with_a_screen_gets_the_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    async def notify(event: Any) -> None:
        sent.append(event)

    monkeypatch.setattr(live, "notify", notify)
    ids = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    hook = ex_ports.screen_hook(True, *ids)
    assert hook is not None
    await hook({"verb": "click", "url": URL})
    assert isinstance(sent[0], ev.ScreenAction) and sent[0].task_id == ids[0]
    assert ex_ports.screen_hook(False, *ids) is None


@pytest.mark.parametrize("url", ["data:text/html,<h1>secret</h1>", "about:blank", "javascript:alert(1)"])
async def test_a_non_web_url_publishes_no_host_or_path(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    sent: list[Any] = []

    async def notify(event: Any) -> None:
        sent.append(event)

    monkeypatch.setattr(live, "notify", notify)
    publish = ex_ports.screen_actions(TaskId(uuid.uuid4()), StepId(uuid.uuid4()), None)
    await publish({"verb": "open", "url": url})
    assert (sent[0].host, sent[0].path) == ("", "")
