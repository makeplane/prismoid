# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The agent yields: before any browser call dispatches - the inspect `guard_action`
makes included - a live control lease parks the call (`CallDeferred`, kind `browser_control`),
and `browser.handoff(reason)` asks for a person (kind `browser_handoff`). Neither acts on the
page (a handoff only reads where it is). The engine's pure halves - the classification, the three
resume texts, the park's hold - are tested here too; the gate's round trip is
`tests/integration/gates/test_control_gate.py`."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.ask import ASK_USER, REQUEST_CONNECTOR
from prismoid.ai.tools.browser import (
    BROWSER_TOOLS,
    CONTROL_HELD,
    CONTROL_UNKNOWN,
    HANDOFF,
    HANDOFF_UNAVAILABLE,
    BrowserPorts,
    browser_toolset,
)
from prismoid.operations import execution
from prismoid.operations.execution import answers as ex_answers
from prismoid.operations.execution import deferrals as ex_deferrals
from prismoid.operations.execution import ports as ex_ports
from prismoid.operations.execution import wake as ex_wake
from prismoid.schemas.screen import HANDOFF_REASON_MAX, ControlState, ControlUnknown

ADA = ControlState(holder_name="Ada", private=True, since=dt.datetime(2026, 10, 2, 9, 0, tzinfo=dt.UTC))


class FakeSidecar:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(cmd)
        if cmd["verb"] == "inspect":
            return {
                "ok": True,
                "role": "button",
                "name": "Save",
                "host": "admin.example.com",
                "path": "/users",
                "form_labels": [],
                "submit_name": "",
            }
        return {"ok": True, "url": "https://admin.example.com/users", "title": "Users"}


class FakeControl:
    """`BrowserPorts.control`: the lease as the hook reads it - flipped by the test."""

    def __init__(self, state: ControlState | None = None) -> None:
        self.state = state
        self.reads = 0

    async def __call__(self) -> ControlState | None:
        self.reads += 1
        return self.state


def _ports(sidecar: FakeSidecar, control: FakeControl | None) -> BrowserPorts:
    async def allow(domains: list[str]) -> None:
        return None

    return BrowserPorts(browse=sidecar, allow=allow, allowed={"admin.example.com"}, control=control)


def _returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def _agent(toolset: FunctionToolset[Any] | None, tool: str, args: dict[str, Any]) -> Agent[None, Any]:
    """One call to `tool`, then the last tool result echoed back as the final text."""
    assert toolset is not None

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    return Agent(FunctionModel(script), output_type=[str, DeferredToolRequests], toolsets=[toolset])


VERBS: list[tuple[str, dict[str, Any]]] = [
    ("browser_open", {"url": "https://admin.example.com/users"}),
    ("browser_back", {}),
    ("browser_wait", {"for_text": "Saved"}),
    ("browser_snapshot", {}),
    ("browser_click", {"ref": "e1.1"}),
    ("browser_type", {"ref": "e1.3", "text": "hunter2"}),
    ("browser_fill", {"fields": [{"ref": "e1.3", "value": "hunter2"}]}),
    ("browser_select", {"ref": "e1.4", "value": "open"}),
    ("browser_press", {"key": "Tab"}),
    ("browser_scroll", {"direction": "down"}),
    ("browser_screenshot", {}),
    ("browser_handoff", {"reason": "sign in to admin.example.com"}),
]


def test_handoff_joins_the_family_and_the_operator_holds_it_on_the_browser() -> None:
    from prismoid.ai.agents import all as _all  # the import IS the registration
    from prismoid.ai.agents.base import browsing
    from prismoid.schemas.ids import AgentName

    assert "browser.handoff" in BROWSER_TOOLS
    operator = _all.REGISTRY[AgentName("operator")]
    assert "browser.handoff" in browsing(operator, on=True).tools
    assert "browser.handoff" not in operator.tools


@pytest.mark.parametrize(("tool", "args"), VERBS, ids=[t for t, _ in VERBS])
async def test_a_live_lease_parks_every_browser_call_before_the_sidecar(
    tool: str, args: dict[str, Any]
) -> None:
    sidecar, control = FakeSidecar(), FakeControl(ADA)
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, control), Approvals(tainted=tainted))
    out = await _agent(toolset, tool, args).run("go")
    assert isinstance(out.output, DeferredToolRequests)
    [call] = out.output.calls
    assert call.tool_name == tool and not out.output.approvals
    assert out.output.metadata[call.tool_call_id] == {
        "kind": CONTROL_HELD,
        "holder": "Ada",
        "private": True,
        "since": ADA.since.isoformat(),
    }
    assert sidecar.calls == [], "a held call never reaches the sidecar - not even guard_action's inspect"
    assert control.reads == 1


class UnreadableControl(FakeControl):
    """The lease could not be read (the port has already retried)."""

    async def __call__(self) -> ControlState | None:
        self.reads += 1
        raise ControlUnknown


@pytest.mark.parametrize(("tool", "args"), VERBS, ids=[t for t, _ in VERBS])
async def test_a_lease_that_cannot_be_read_refuses_the_call_and_never_parks(
    tool: str, args: dict[str, Any]
) -> None:
    """A person may be signing in privately: a snapshot would put what they typed into the
    model's context, a click could submit their half-filled form. Unknown is not "nobody" - the
    call is refused, the run goes on, and nothing waits on a gate no lease will ever answer."""
    sidecar, control = FakeSidecar(), UnreadableControl()
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, control), Approvals(tainted=tainted))
    out = await _agent(toolset, tool, args).run("go")
    assert out.output == CONTROL_UNKNOWN
    assert sidecar.calls == [], "an unconfirmed call never reaches the sidecar - not even inspect"
    assert control.reads == 1


async def test_the_lease_read_is_retried_once_then_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[int] = []

    async def flaky(task_id: object) -> ControlState | None:
        reads.append(1)
        if len(reads) == 1:
            raise ConnectionError("a pool timeout")
        return ADA

    monkeypatch.setattr(execution.machines.screen, "control_state", flaky)
    assert await ex_ports.lease_now(uuid.uuid4()) == ADA
    assert len(reads) == 2

    async def down(task_id: object) -> ControlState | None:
        reads.append(1)
        raise ConnectionError("the database went away")

    reads.clear()
    monkeypatch.setattr(execution.machines.screen, "control_state", down)
    with pytest.raises(ControlUnknown):
        await ex_ports.lease_now(uuid.uuid4())
    assert len(reads) == 2


async def test_nobody_driving_lets_every_call_through() -> None:
    """The hook reads the lease and finds nobody: the call runs as before."""
    sidecar, control = FakeSidecar(), FakeControl()
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, control), Approvals(tainted=tainted))
    out = await _agent(toolset, "browser_snapshot", {}).run("go")
    assert isinstance(out.output, str)
    assert [c["verb"] for c in sidecar.calls] == ["snapshot"]
    assert control.reads == 1


async def test_handoff_asks_for_a_person_with_a_capped_one_line_reason() -> None:
    sidecar = FakeSidecar()
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, FakeControl()), Approvals(tainted=tainted))
    reason = "sign in to admin.example.com\r\n\x1b[2J" + "x" * 300
    out = await _agent(toolset, "browser_handoff", {"reason": reason}).run("go")
    assert isinstance(out.output, DeferredToolRequests)
    [call] = out.output.calls
    meta = out.output.metadata[call.tool_call_id]
    assert meta["kind"] == HANDOFF
    assert meta["reason"].startswith("sign in to admin.example.com ")
    assert len(meta["reason"]) <= HANDOFF_REASON_MAX
    assert "\x1b" not in meta["reason"] and "\n" not in meta["reason"] and "\r" not in meta["reason"]


async def test_handoff_carries_the_host_the_sidecar_says_the_page_is_on() -> None:
    """The people asked to sign in are told where the page is by the sidecar, never only by the
    agent's own words: one read-only inspect, no verb, nothing journaled."""
    sidecar = FakeSidecar()
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, FakeControl()), Approvals(tainted=tainted))
    out = await _agent(toolset, "browser_handoff", {"reason": "sign in to bank.example.com"}).run("go")
    assert isinstance(out.output, DeferredToolRequests)
    [call] = out.output.calls
    assert out.output.metadata[call.tool_call_id]["host"] == "admin.example.com"
    assert sidecar.calls == [{"verb": "inspect"}]


async def test_a_handoff_whose_page_cannot_be_read_still_asks_with_no_host() -> None:
    class Unreadable(FakeSidecar):
        async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(cmd)
            raise ConnectionError("the sidecar is restarting")

    toolset = browser_toolset(
        BROWSER_TOOLS, None, _ports(Unreadable(), FakeControl()), Approvals(tainted=tainted)
    )
    out = await _agent(toolset, "browser_handoff", {"reason": "sign in"}).run("go")
    assert isinstance(out.output, DeferredToolRequests)
    [call] = out.output.calls
    assert out.output.metadata[call.tool_call_id]["host"] == ""


async def test_a_handoff_on_the_device_gives_the_person_the_window_last() -> None:
    """After the page read: anything of the agent's that reaches the frame ends the hand-off."""
    order: list[str] = []

    class Reads(FakeSidecar):
        async def __call__(self, cmd: dict[str, Any]) -> dict[str, Any]:
            order.append(str(cmd["verb"]))
            return await super().__call__(cmd)

    async def hand_over() -> None:
        order.append("hand_over")

    ports = replace(_ports(Reads(), FakeControl()), hand_over=hand_over)
    out = await _agent(
        browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted)),
        "browser_handoff",
        {"reason": "sign in"},
    ).run("go")
    assert isinstance(out.output, DeferredToolRequests)
    assert order[-1] == "hand_over"


async def test_a_hand_over_that_fails_still_hands_off() -> None:
    async def hand_over() -> None:
        raise ConnectionError("the helper went away")

    ports = replace(_ports(FakeSidecar(), FakeControl()), hand_over=hand_over)
    out = await _agent(
        browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted)),
        "browser_handoff",
        {"reason": "sign in"},
    ).run("go")
    assert isinstance(out.output, DeferredToolRequests)


async def test_a_browser_nobody_can_see_answers_the_handoff_itself() -> None:
    """A seat's throwaway browser (no Machine, so `control` is None) has no screen: a gate there
    would wait for a person who can never take over."""
    sidecar = FakeSidecar()
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, None), Approvals(tainted=tainted))
    out = await _agent(toolset, "browser_handoff", {"reason": "sign in"}).run("go")
    assert out.output == HANDOFF_UNAVAILABLE
    assert sidecar.calls == []


async def test_an_approved_call_still_yields_to_a_person_and_never_acts() -> None:
    """A person took control while a danger ask waited: the approved click is deferred again,
    never run - the person's page is not the one the approver saw."""
    sidecar, control = FakeSidecar(), FakeControl()
    toolset = browser_toolset(BROWSER_TOOLS, None, _ports(sidecar, control), Approvals(tainted=tainted))
    agent = _agent(toolset, "browser_click", {"ref": "e1.1", "confirm": True})
    asked = await agent.run("go")
    assert isinstance(asked.output, DeferredToolRequests) and asked.output.approvals
    call_id = asked.output.approvals[0].tool_call_id
    fingerprint = asked.output.metadata[call_id]["fingerprint"]
    control.state = ADA
    resumed = await agent.run(
        message_history=asked.all_messages(),
        deferred_tool_results=DeferredToolResults(
            approvals={
                call_id: ToolApproved(override_args={"ref": "e1.1", "confirm": True, "expect": fingerprint})
            }
        ),
    )
    assert isinstance(resumed.output, DeferredToolRequests)
    assert [c.tool_call_id for c in resumed.output.calls] == [call_id]
    assert resumed.output.metadata[call_id]["kind"] == CONTROL_HELD
    assert [c["verb"] for c in sidecar.calls] == ["inspect"], "the click never ran"


def _deferred(
    calls: list[tuple[str, str, dict[str, Any] | None]], approvals: tuple[str, ...] = ()
) -> DeferredToolRequests:
    """A hand-built turn: (tool_call_id, tool_name, metadata) per external call."""
    return DeferredToolRequests(
        calls=[ToolCallPart(tool_name=name, args={}, tool_call_id=cid) for cid, name, _ in calls],
        approvals=[ToolCallPart(tool_name="browser_click", args={}, tool_call_id=a) for a in approvals],
        metadata={cid: meta for cid, _, meta in calls if meta is not None},
    )


def test_one_control_call_parks_and_every_other_pending_id_is_carried() -> None:
    held = {"kind": CONTROL_HELD, "holder": "Ada", "private": False, "since": "2026-10-02T09:00:00+00:00"}
    output = _deferred(
        [
            ("ask-1", ASK_USER, {"kind": ASK_USER, "questions": []}),
            ("snap-1", "browser_snapshot", held),
            ("conn-1", REQUEST_CONNECTOR, {"kind": REQUEST_CONNECTOR}),
            ("click-1", "browser_click", held),
        ],
        approvals=("appr-1",),
    )
    assert [c.tool_call_id for c in ex_deferrals.control_calls(output)] == ["snap-1", "click-1"]
    deferral = ex_deferrals.control_deferral(output)
    assert (deferral.tool_call_id, deferral.source, deferral.holder) == ("snap-1", "takeover", "Ada")
    assert deferral.since == "2026-10-02T09:00:00+00:00" and deferral.reason == ""
    assert deferral.extra_calls == ("click-1", "conn-1")
    assert deferral.extra_asks == ("ask-1",)
    assert deferral.denied_approvals == ("appr-1",)


def test_a_handoff_parks_with_its_reason_and_a_plain_turn_has_no_control_call() -> None:
    meta = {"kind": HANDOFF, "reason": "sign in to admin.example.com", "host": "admin.example.com"}
    output = _deferred([("hand-1", "browser_handoff", meta)])
    deferral = ex_deferrals.control_deferral(output)
    assert (deferral.source, deferral.reason, deferral.holder, deferral.host) == (
        "handoff",
        "sign in to admin.example.com",
        "",
        "admin.example.com",
    )
    plain = _deferred([("ask-1", ASK_USER, {"kind": ASK_USER})])
    assert ex_deferrals.control_calls(plain) == []
    with pytest.raises(ValueError):
        ex_deferrals.control_deferral(plain)


def test_a_held_call_outranks_the_agents_own_handoff_in_one_turn() -> None:
    """Someone is already driving: the step parks as a takeover (unbudgeted), never as the
    handoff that happened to come first, and the handoff rides `extra_calls`."""
    held = {"kind": CONTROL_HELD, "holder": "Ada", "private": False, "since": "2026-10-02T09:00:00+00:00"}
    output = _deferred(
        [
            ("hand-1", "browser_handoff", {"kind": HANDOFF, "reason": "sign in to admin.example.com"}),
            ("snap-1", "browser_snapshot", held),
        ]
    )
    deferral = ex_deferrals.control_deferral(output)
    assert (deferral.tool_call_id, deferral.source, deferral.holder) == ("snap-1", "takeover", "Ada")
    assert deferral.since == "2026-10-02T09:00:00+00:00" and deferral.reason == ""
    assert deferral.extra_calls == ("hand-1",)


def test_the_held_ids_are_answered_not_run() -> None:
    """Every other pending id of the parked turn is answered `NOT_RUN_FOR_CONTROL` on resume:
    calls and asks as text, approvals as a denial (pydantic-ai refuses partial results)."""
    deferral = execution.ControlDeferral(
        tool_call_id="snap-1",
        source="takeover",
        reason="",
        holder="Ada",
        extra_calls=("click-1", "conn-1"),
        denied_approvals=("appr-1",),
        extra_asks=("ask-1",),
    )
    held = ex_answers.answers_to(deferral, DeferredToolRequests(), {"answer": "back"})
    assert held.calls == {
        "snap-1": "back",
        "click-1": execution.NOT_RUN_FOR_CONTROL,
        "conn-1": execution.NOT_RUN_FOR_CONTROL,
        "ask-1": execution.NOT_RUN_FOR_CONTROL,
    }
    assert held.approvals == {"appr-1": ToolDenied(execution.NOT_RUN_FOR_CONTROL)}
    # Past the handoff budget nobody took the browser: the others say what really happened.
    refused = ex_answers.answers_to(
        deferral, DeferredToolRequests(), {"held_text": execution.NOT_RUN_BESIDE_HANDOFF}
    )
    assert refused.calls["click-1"] == execution.NOT_RUN_BESIDE_HANDOFF
    assert "handed to a person" not in execution.NOT_RUN_BESIDE_HANDOFF


def test_the_three_resume_texts_are_the_specs() -> None:
    accepted = {"outcome": "accepted", "holder": "Ada", "held_s": 100, "reason": "released"}
    assert execution.control_answer("takeover", "accept", accepted, wait_minutes=20) == (
        "Ada drove the browser for 1m 40s and handed back control. The page may have changed: take "
        "a new snapshot before acting."
    )
    handed = {"outcome": "accepted", "holder": "Ada", "held_s": 130, "reason": "released"}
    assert execution.control_answer("handoff", "accept", handed, wait_minutes=20) == (
        "Ada took over and handed back after 2m 10s. Take a new snapshot to see where things stand."
    )
    skipped = {"outcome": "declined", "by": "Bob"}
    assert execution.control_answer("handoff", "decline", skipped, wait_minutes=20) == (
        "Nobody took over (Bob skipped). Continue without it if you can, or stop and say what is needed."
    )
    assert execution.control_answer("handoff", "decline", {"outcome": "timeout"}, wait_minutes=20) == (
        "Nobody took over (no one came in 20 minutes). Continue without it if you can, or stop and "
        "say what is needed."
    )


def test_the_kill_switch_declines_with_its_own_reason() -> None:
    """A lease ended by the switch DECLINES the gate (never accepts it) - the parked
    call says computer use is off, not that someone handed back."""
    disabled = {"outcome": "disabled", "reason": "Computer use is off."}
    assert execution.control_answer("takeover", "decline", disabled, wait_minutes=20) == (
        "Nobody took over (computer use is off). Continue without it if you can, or stop and say "
        "what is needed."
    )


class _HoldFrame:
    def __init__(self) -> None:
        self.verbs: list[dict[str, Any]] = []

    async def browser(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.verbs.append(cmd)
        return {"ok": True, "held": True}


async def test_a_control_park_pins_chromium_and_carries_its_once_widens() -> None:
    frame = _HoldFrame()
    ports = _ports(FakeSidecar(), FakeControl())
    ports.allowed.update({"once.example.com", "127.0.0.1"})
    deferral = execution.ControlDeferral(tool_call_id="c", source="takeover", reason="", holder="Ada")
    vouched = {"admin.example.com"}  # what the allowlist's homes said when the run began
    parked = await execution._browser_park(frame, ports, deferral, vouched=vouched)  # pyright: ignore[reportPrivateUsage]
    assert frame.verbs == [{"verb": "hold"}]
    assert parked.browser_once == ("once.example.com",)


def test_a_resumed_control_park_says_what_actually_happened() -> None:
    """The `step.running` anchor never says "the browser was handed back" when nobody drove - in
    particular when the kill switch declined the gate."""
    detail = ex_wake.resumed_detail
    control = {"control": {"source": "takeover", "holder": "Ada"}}
    assert detail({**control, "control_outcome": "accepted"}) == "resumed: the browser was handed back"
    assert detail({**control, "control_outcome": "disabled"}) == (
        "resumed: computer use is off, so nobody took over the browser"
    )
    for outcome in ("declined", "timeout"):
        assert detail({**control, "control_outcome": outcome}) == "resumed: nobody took over the browser"
    assert detail({"approval_action": "accept"}) == "resumed with the user's decision"
    assert detail({"answer": "42"}) == "resumed with the user's answer"
