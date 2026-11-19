# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Approvals as data: a person approves exactly the acts that matter (irreversible, after outside text,
widening egress), an unattended run proceeds, deny beats ask beats allow, and a bad rule fails at load."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_ai import DeferredToolRequests
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.frames.image_browser.sidecar.scripts import INSPECT_JS
from prismoid.ai.rules.approvals import ApprovalRules, Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, browser_toolset
from prismoid.schemas.approvals import RULES, Act, Call, Decision, Door, Match, Rule
from prismoid.schemas.side_effects import SideEffect
from tests.support.fakes.browser_fakes import FakeSidecar, agent, journal, ports, returns

WRITE = Act(door=Door.CONNECTOR, effect=SideEffect.WORLD)
READ = Act(door=Door.CONNECTOR, effect=SideEffect.READ)
CARD = {"tool": "gmail_send", "preview": "to: a@b.c"}


class Shop(FakeSidecar):
    """A product page: "Add to cart" submits a form whose other button is "Buy now", as the sidecar
    reads it (fields only); "Place your order" is the checkout's act."""

    def target(self, ref: str, value: object = None) -> dict[str, Any]:
        name = {"e2.1": "Add to cart", "e2.2": "Place your order"}.get(ref)
        if name is None:
            return super().target(ref, value)
        at = {"host": "shop.example.com", "path": "/item"}
        return {"ok": True, "role": "button", "name": name, "submits": True, "submit_name": name, **at} | {
            "form_labels": ["Quantity"],
            "option": "",
        }


async def _click(ref: str) -> tuple[Any, Shop]:
    sidecar = Shop()
    toolset = browser_toolset(
        BROWSER_TOOLS, journal([]), ports(sidecar, {"shop.example.com"}, []), Approvals(tainted=tainted)
    )

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not returns(messages):
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_click", args={"ref": ref})])
        return ModelResponse(parts=[TextPart("done")])

    return (await agent(toolset, script).run("buy the kettle")).output, sidecar


async def test_add_to_cart_acts_and_only_placing_the_order_asks() -> None:
    added, shop = await _click("e2.1")
    assert added == "done" and any(c["verb"] == "click" for c in shop.calls), (
        "adding to the cart is not the act"
    )
    ordered, shop = await _click("e2.2")
    assert isinstance(ordered, DeferredToolRequests), "placing the order is the irreversible act"
    assert not any(c["verb"] == "click" for c in shop.calls), "no click before the person's yes"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JavaScript")
def test_the_form_labels_are_the_fields_never_another_button_or_a_hidden_input() -> None:
    script = f"""
const fn = ({INSPECT_JS});
const field = (type, label, name = "") => ({{
  type, name, labels: label ? [{{textContent: label}}] : null, getAttribute: () => null,
}});
const cart = {{tagName: "INPUT", type: "submit", value: "Add to cart", getAttribute: () => null}};
const form = {{
  getAttribute: () => null,
  querySelectorAll: () => [
    field("select-one", "Quantity"), field("hidden", null, "submit.buy-now"), field("submit", "Buy now"),
  ],
  querySelector: () => cart,
}};
console.log(JSON.stringify(fn.call({{...cart, form, closest: () => null}})));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    got = json.loads(out.stdout)
    assert got["form_labels"] == ["Quantity"] and got["submit_name"] == "Add to cart" and got["submits"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JavaScript")
def test_the_form_labels_are_what_the_form_sends_never_an_unticked_option() -> None:
    """A product form's unticked "Add gift options at checkout" box is not sent, so it never names
    the act; a ticked one, and a filled field, are."""
    script = f"""
const fn = ({INSPECT_JS});
const field = (type, label, extra = {{}}) => ({{
  type, name: "", labels: [{{textContent: label}}], getAttribute: () => null, ...extra,
}});
const cart = {{tagName: "INPUT", type: "submit", value: "Add to cart", getAttribute: () => null}};
const form = {{
  getAttribute: () => null,
  querySelectorAll: () => [
    field("select-one", "Quantity"),
    field("checkbox", "Add gift options at checkout", {{checked: false}}),
    field("radio", "Pay on delivery", {{checked: false}}),
    field("checkbox", "Gift wrap", {{checked: true}}),
    field("text", "Coupon", {{disabled: true}}),
  ],
  querySelector: () => cart,
}};
console.log(JSON.stringify(fn.call({{...cart, form, closest: () => null}})));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout)["form_labels"] == ["Quantity", "Gift wrap"]


def _inspect(setup: str, call: str) -> dict[str, Any]:
    script = f"""
globalThis.location = {{href: "https://shop.example.com/item?id=1"}};
const fn = ({INSPECT_JS});
const attrs = (a) => (k) => (k in a ? a[k] : null);
{setup}
console.log(JSON.stringify({call}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JavaScript")
@pytest.mark.parametrize(
    ("href", "extra", "navigates"),
    [
        ("https://rbi.org.in/bulletin.pdf", {}, True),
        ("/us/shop/buy-iphone", {}, True),
        ("#", {}, False),
        ("#reviews", {}, False),
        ("javascript:void(0)", {}, False),
        ("/us/shop/buy-iphone", {"onclick": "buy()"}, False),
        (None, {}, False),
    ],
)
def test_a_link_navigates_only_when_its_click_can_only_load_another_page(
    href: str | None, extra: dict[str, str], navigates: bool
) -> None:
    a = json.dumps({"href": href, **extra} if href is not None else extra)
    resolved = json.dumps(href or "")
    setup = f"""const link = {{tagName: "A", href: new URL({resolved}, location.href).href,
  getAttribute: attrs({a}), closest: () => null}};"""
    assert _inspect(setup, "fn.call(link)")["navigates"] is navigates


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JavaScript")
@pytest.mark.parametrize(
    ("form_attrs", "button_attrs", "sends_by_get"),
    [
        ({"method": "get"}, {}, True),
        ({"method": "GET"}, {}, True),
        ({}, {}, False),  # no method said: a script may well post it
        ({"method": "post"}, {}, False),
        ({"method": "get", "onsubmit": "pay()"}, {}, False),
        ({"method": "get"}, {"formmethod": "post"}, False),
    ],
)
def test_a_form_sends_by_get_only_when_the_page_says_so(
    form_attrs: dict[str, str], button_attrs: dict[str, str], sends_by_get: bool
) -> None:
    setup = f"""const go = {{tagName: "BUTTON", type: "submit", textContent: "Submit Search",
  getAttribute: attrs({json.dumps(button_attrs)})}};
const form = {{getAttribute: attrs({json.dumps(form_attrs)}), querySelectorAll: () => [], querySelector: () => go}};
const box = {{tagName: "INPUT", type: "search", form, getAttribute: () => null, closest: () => null}};"""
    got = _inspect(setup, "fn.call(box)")
    assert got["form_navigates"] is sends_by_get and got["navigates"] is False


def test_a_connector_write_after_outside_text_asks_on_an_interactive_task() -> None:
    approve = Approvals(tainted=lambda: True)
    with pytest.raises(ApprovalRequired) as asked:
        approve(WRITE, False, CARD)
    assert asked.value.metadata == {
        **CARD,
        "rule": "write-after-outside-text",
        "why": "this task read outside text",
    }, "the door's card reaches the gate, with the rule that asked"
    assert approve(READ, False, CARD) is None, "a read after outside text runs"
    assert Approvals(tainted=tainted)(WRITE, False, CARD) is None, "a write on a connector you connected runs"


def test_an_unattended_run_proceeds_without_asking() -> None:
    unattended = Approvals(attended=False, tainted=lambda: True)
    assert unattended(WRITE, False, CARD) is None
    destructive = WRITE.model_copy(update={"irreversible": True})
    assert unattended(destructive, False, CARD) is None
    with pytest.raises(ApprovalRequired):
        Approvals(tainted=tainted)(destructive, False, CARD)
    buy = Act(door=Door.BROWSER, effect=SideEffect.WORLD, irreversible=True)
    for still_asks in (buy, WRITE.model_copy(update={"owner_asks": True})):
        with pytest.raises(ApprovalRequired):
            unattended(still_asks, False, CARD)  # the owner's inbox hears it; its timeout denies


def test_a_seat_that_cannot_ask_is_refused_and_an_approved_call_runs() -> None:
    refused = Approvals(can_ask=False, tainted=lambda: True)(WRITE, False, CARD)
    assert refused is not None and "read outside text" in refused and "cannot ask" in refused
    assert Approvals(tainted=lambda: True)(WRITE, True, CARD) is None


def test_deny_beats_ask_beats_allow() -> None:
    call = Call(door=Door.CONNECTOR, effect=SideEffect.WORLD, owner_asks=True)

    def rule(id: str, decision: Decision) -> Rule:
        return Rule(
            id=id,
            decision=decision,
            when=Match(owner_asks=True),
            why=id,
            hits=(call,),
            misses=(Call(**READ.model_dump()),),
        )

    allow, ask, deny = rule("allow", Decision.ALLOW), rule("ask", Decision.ASK), rule("deny", Decision.DENY)
    assert ApprovalRules((allow, deny, ask)).decide(call) == deny
    assert ApprovalRules((allow, ask)).decide(call) == ask
    assert Approvals(tainted=tainted, rules=ApprovalRules((allow, deny)))(
        WRITE.model_copy(update={"owner_asks": True}), True, CARD
    ) == ("refused: deny"), "a deny holds even for an approved call"
    assert (
        Approvals(tainted=tainted, rules=ApprovalRules((allow,)))(
            WRITE.model_copy(update={"owner_asks": True}), False, CARD
        )
        is None
    )


def test_a_malformed_rule_fails_at_load() -> None:
    hit = Call(door=Door.BROWSER, effect=SideEffect.WORLD, irreversible=True)
    miss = Call(door=Door.BROWSER, effect=SideEffect.WORLD)
    good = {
        "id": "x",
        "decision": "ask",
        "when": {"irreversible": True},
        "why": "x",
        "hits": [hit],
        "misses": [miss],
    }
    Rule.model_validate(good)
    for bad in (
        good | {"hits": [miss]},  # misses a call it must match
        good | {"misses": [hit]},  # matches a call it must miss
        good | {"when": {}},  # keys on nothing
        good | {"decision": "maybe"},
        good | {"when": {"recipient": "anyone"}},
        good | {"hits": []},
    ):
        with pytest.raises(ValidationError):
            Rule.model_validate(bad)
    with pytest.raises(ValueError, match="repeat"):
        ApprovalRules((RULES[0], RULES[0]))
