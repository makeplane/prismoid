# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Approvals: a declined browser act is not asked again (and only that act is refused), an
ask carries the rule that asked, a card names the items the run has seen, and moving money asks."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS
from prismoid.ai.tools.browser_policy import act_target
from prismoid.ai.tools.call_rows import DECLINED, declined_text
from prismoid.operations.execution.answers import verdicts
from prismoid.operations.execution.deferrals import ApprovalDeferral
from prismoid.schemas.approvals import Act, Door, moves_money
from prismoid.schemas.connectors import args_preview, named_ids
from prismoid.schemas.side_effects import SideEffect
from tests.support.fakes.browser_fakes import FakeSidecar, hands
from tests.support.fakes.browser_fakes import agent as _agent
from tests.support.fakes.browser_fakes import journal as _journal
from tests.support.fakes.browser_fakes import ports as _ports
from tests.support.fakes.browser_fakes import returns as _returns


async def _deny_then(retry_ref: str) -> tuple[Any, FakeSidecar]:
    """Snapshot, ask on 'Delete all orders', the person's Deny (as the engine answers it), then a click
    on `retry_ref`: the second run's output and the sidecar."""
    sidecar, rows = FakeSidecar(), []
    toolset = hands(BROWSER_TOOLS, _journal(rows), _ports(sidecar, {"admin.example.com"}, []))

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = _returns(messages)
        if not seen:
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_snapshot", args={})])
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_click", args={"ref": "e1.2"})])
        if len(seen) == 2:
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_click", args={"ref": retry_ref})])
        return ModelResponse(parts=[TextPart(str(seen[-1].content))])

    agent = _agent(toolset, script)
    first = await agent.run("clean up")
    assert isinstance(first.output, DeferredToolRequests)
    call_id = first.output.approvals[0].tool_call_id
    no = declined_text(act_target(first.output.metadata[call_id]["fingerprint"]))
    second = await agent.run(
        message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={call_id: ToolDenied(no)}),
    )
    return second, sidecar


async def test_a_declined_browser_click_is_not_asked_again() -> None:
    second, sidecar = await _deny_then("e1.2")
    assert not isinstance(second.output, DeferredToolRequests), "a declined browser act asked again"
    assert DECLINED in str(second.output)
    assert all(c["verb"] != "click" for c in sidecar.calls)


async def test_a_decline_refuses_only_the_act_declined() -> None:
    second, sidecar = await _deny_then("e1.1")  # 'Save changes': another act, no danger word
    assert not isinstance(second.output, DeferredToolRequests)
    assert [c["ref"] for c in sidecar.calls if c["verb"] == "click"] == ["e1.1"]


def test_an_ask_carries_the_rule_that_asked() -> None:
    approvals = Approvals(tainted=lambda: True)
    act = Act(door=Door.CONNECTOR, effect=SideEffect.WORLD)
    with pytest.raises(ApprovalRequired) as raised:
        approvals(act, False, {"tool": "linear_update_issue", "preview": "id: LIN-1"})
    meta = raised.value.metadata or {}
    assert meta.get("rule") == "write-after-outside-text", meta
    assert meta.get("why") == "this task read outside text", meta


def test_a_card_names_the_ids_the_run_has_seen() -> None:
    order = {
        "id": "#W5810021",
        "status": "delivered",
        "items": [
            {"item_id": "7001", "name": "Bluetooth speaker"},
            {"item_id": "7002", "name": "USB-C cable 1m"},
        ],
        "payment": "credit_card_7781",
    }
    names = named_ids(order)
    assert names == {"7001": "Bluetooth speaker", "7002": "USB-C cable 1m"}
    preview = args_preview({"item_ids": ["7002"], "order_id": "#W5810021"}, names=names)
    assert "item_ids: ['7002'] (USB-C cable 1m)" in preview
    assert "order_id: #W5810021" in preview.splitlines()


def test_a_card_names_no_id_two_items_share_and_no_amount() -> None:
    names = named_ids([{"id": 1, "name": "Savings"}, {"id": 2, "name": "Checking"}])
    named_ids([{"id": 2, "name": "Unknown Ltd"}], names)
    preview = args_preview({"from_account_id": 1, "payee_id": 2, "amount": 1}, names=names)
    assert preview.splitlines() == ["amount: 1", "from_account_id: 1 (Savings)", "payee_id: 2"]


@pytest.mark.parametrize(
    ("tool", "pays"),
    [
        ("send_money", True),
        ("createPayment", True),
        ("transfer_funds", True),
        ("get_balance", False),
        ("return_items", False),
    ],
)
def test_a_tool_name_says_whether_it_moves_money(tool: str, pays: bool) -> None:
    assert moves_money(tool) is pays


def test_moving_money_asks_on_an_untainted_connected_run() -> None:
    """The live bug: an explicitly connected bank's send_money, first call of the run, ran unasked."""
    approvals = Approvals(tainted=lambda: False)
    act = Act(door=Door.CONNECTOR, effect=SideEffect.WORLD, moves_money=moves_money("send_money"))
    with pytest.raises(ApprovalRequired) as raised:
        approvals(act, False, {"tool": "bank_send_money", "preview": "amount: 50.0"})
    assert (raised.value.metadata or {}).get("why") == "it moves money"
    assert approvals(Act(door=Door.CONNECTOR, effect=SideEffect.WORLD), False, {}) is None


def test_the_engine_marks_a_browser_deny_with_the_act_and_a_connector_deny_plainly() -> None:
    fp = {"role": "button", "name": "Submit order", "host": "httpbin.org", "path": "/forms/post"}
    deferral = ApprovalDeferral(
        items=[
            ("c1", "httpbin.org", "browser.click", "click"),
            ("c2", "evil.example", "browser.open", "open"),
            ("c3", "Store", "store_return_items", "item_ids: ['7002']"),
        ],
        fingerprints={"c1": fp, "c2": None, "c3": None},
    )
    out = verdicts(deferral, DeferredToolRequests(), {"approval_action": "decline"})
    assert out["c1"] == ToolDenied(declined_text(act_target(fp)))
    assert out["c2"] == ToolDenied(declined_text("evil.example"))
    assert out["c3"] == ToolDenied(DECLINED)
