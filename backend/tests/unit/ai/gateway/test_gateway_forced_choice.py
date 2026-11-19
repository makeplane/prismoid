# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The self-healing forced `tool_choice` fallback.

A model that rejects a forced `tool_choice` (`{"type": "tool"}` / `{"type": "any"}`) answers
400 "tool_choice: type "tool" and "any" are not supported for this model." for every agent with a
structured (tool) output - the planner first among them, which then fell to the one-step floor.
The gateway's Anthropic constructor remembers the model, rebuilds it with forcing off (pydantic-ai
then asks with `tool_choice: auto`) and retries THAT request once. Nothing else is retried.

No network: a fake Anthropic HTTP transport answers the 400 for a forced choice and succeeds
for auto, and records every request body it was sent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError

from prismoid.ai.gateway import providers, throttle
from prismoid.schemas.model_class import Protocol
from prismoid.schemas.plan import PlanProposal

REFUSAL = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
    },
}
OTHER_400 = {
    "type": "error",
    "error": {"type": "invalid_request_error", "message": "messages: at least one message is required"},
}
# Names tool_choice and "not supported" - but not the forced types: never this model's refusal.
NEAR_MISS_400 = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "none" is not supported for this model.',
    },
}


class Verdict(BaseModel):
    answer: str


@pytest.fixture(autouse=True)
def fresh_memory() -> Iterator[None]:
    providers._FORCED_CHOICE_REFUSED.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    providers._FORCED_CHOICE_REFUSED.clear()  # pyright: ignore[reportPrivateUsage]


class FakeAnthropic:
    """The Messages API, minus the model: answers each request with the tool call the script says
    (the request's own first tool, so the output tool's name is never guessed), or text."""

    def __init__(
        self, *, rejects_forcing: bool, other_400: dict[str, Any] | bool = False, args: Any = None
    ) -> None:
        self.rejects_forcing = rejects_forcing
        self.other_400 = OTHER_400 if other_400 is True else other_400
        self.args = args if args is not None else {"answer": "yes"}
        self.bodies: list[dict[str, Any]] = []
        self.text_first = False

    @property
    def choices(self) -> list[str | None]:
        return [(b.get("tool_choice") or {}).get("type") for b in self.bodies]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.other_400:
            return httpx2.Response(400, json=self.other_400)
        if self.rejects_forcing and (body.get("tool_choice") or {}).get("type") in ("tool", "any"):
            return httpx2.Response(400, json=REFUSAL)
        if "output_config" in body:  # native structured output (pydantic-ai >=2.52 on models that have it)
            return self._reply(body, [{"type": "text", "text": json.dumps(self.args)}], "end_turn")
        name = body["tools"][0]["name"]
        if self.text_first:
            self.text_first = False
            content: list[dict[str, Any]] = [{"type": "text", "text": "Here is my plan in prose."}]
            stop = "end_turn"
        else:
            content = [
                {"type": "tool_use", "id": f"toolu_{len(self.bodies)}", "name": name, "input": self.args}
            ]
            stop = "tool_use"
        return self._reply(body, content, stop)

    def _reply(self, body: dict[str, Any], content: list[dict[str, Any]], stop: str) -> httpx2.Response:
        if body.get("stream"):
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=_sse(content, stop)
            )
        return httpx2.Response(
            200,
            json={
                "id": f"msg_{len(self.bodies)}",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": content,
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    def client(self) -> httpx2.AsyncClient:
        # throttle.client_for's explicit timeout: on the default one, anthropic>=1 refuses a
        # non-streaming call whose max_tokens could outlast 10 minutes (Opus/Sonnet 5.5's).
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(self.handler), timeout=httpx2.Timeout(600.0, connect=10.0)
        )


def _sse(content: list[dict[str, Any]], stop: str) -> bytes:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_s",
                "type": "message",
                "role": "assistant",
                "model": "fake",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        }
    ]
    for i, block in enumerate(content):
        if block["type"] == "tool_use":
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events += [
            {"type": "content_block_start", "index": i, "content_block": start},
            {"type": "content_block_delta", "index": i, "delta": delta},
            {"type": "content_block_stop", "index": i},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _model(fake: FakeAnthropic, model_id: str) -> Any:
    return providers._anthropic(model_id, None, "test-key", fake.client())  # pyright: ignore[reportPrivateUsage]


async def test_a_model_that_refuses_forcing_heals_and_the_run_succeeds() -> None:
    fake = FakeAnthropic(rejects_forcing=True)
    agent = Agent(_model(fake, "refuses-forcing-a"), output_type=Verdict)

    result = await agent.run("is it?")

    assert result.output == Verdict(answer="yes")
    assert fake.choices[0] in ("tool", "any"), "the first ask is the profile's own forced choice"
    assert fake.choices[1:] == ["auto"], "exactly one failed call, then the same request with auto"


async def test_the_next_run_on_that_model_asks_with_auto_first() -> None:
    fake = FakeAnthropic(rejects_forcing=True)
    await Agent(_model(fake, "refuses-forcing-b"), output_type=Verdict).run("is it?")
    fake.bodies.clear()

    # A fresh build (a new resolve, a new step) and the healed instance itself: no second 400.
    rebuilt = await Agent(_model(fake, "refuses-forcing-b"), output_type=Verdict).run("again?")
    assert rebuilt.output == Verdict(answer="yes")
    assert fake.choices == ["auto"]


async def test_the_healed_instance_stays_healed() -> None:
    fake = FakeAnthropic(rejects_forcing=True)
    model = _model(fake, "refuses-forcing-c")
    await Agent(model, output_type=Verdict).run("is it?")
    fake.bodies.clear()

    await Agent(model, output_type=Verdict).run("again?")
    assert fake.choices == ["auto"]


async def test_an_instance_built_before_another_healed_asks_auto_first() -> None:
    """Two steps resolved the model before either asked: the second one learns from the first."""
    fake = FakeAnthropic(rejects_forcing=True)
    first, second = _model(fake, "refuses-forcing-e"), _model(fake, "refuses-forcing-e")
    await Agent(first, output_type=Verdict).run("is it?")
    fake.bodies.clear()

    await Agent(second, output_type=Verdict).run("again?")
    assert fake.choices == ["auto"]


async def test_the_stream_path_heals_too() -> None:
    fake = FakeAnthropic(rejects_forcing=True)
    agent = Agent(_model(fake, "refuses-forcing-d"), output_type=Verdict)

    async with agent.run_stream("is it?") as run:
        output = await run.get_output()

    assert output == Verdict(answer="yes")
    assert all(b.get("stream") for b in fake.bodies)
    assert fake.choices[0] in ("tool", "any") and fake.choices[1:] == ["auto"]


async def test_a_different_400_is_never_retried() -> None:
    fake = FakeAnthropic(rejects_forcing=False, other_400=True)
    agent = Agent(_model(fake, "other-400"), output_type=Verdict)

    with pytest.raises(ModelHTTPError) as caught:
        await agent.run("is it?")

    assert caught.value.status_code == 400
    assert len(fake.bodies) == 1, "a 400 about anything else is a fact about the request"
    assert "other-400" not in providers._FORCED_CHOICE_REFUSED  # pyright: ignore[reportPrivateUsage]


async def test_a_400_that_names_tool_choice_but_not_the_forced_types_is_never_healed() -> None:
    """Only the exact refusal - type "tool"/"any" not supported - unforces a model."""
    fake = FakeAnthropic(rejects_forcing=False, other_400=NEAR_MISS_400)
    agent = Agent(_model(fake, "near-miss-400"), output_type=Verdict)

    with pytest.raises(ModelHTTPError):
        await agent.run("is it?")

    assert len(fake.bodies) == 1
    assert "near-miss-400" not in providers._FORCED_CHOICE_REFUSED  # pyright: ignore[reportPrivateUsage]


async def test_two_requests_in_flight_on_one_instance_both_recover() -> None:
    """Both forced requests leave before either 400 lands: the first heals the instance, and the
    second - finding it already healed - still retries instead of failing its run."""
    fake = FakeAnthropic(rejects_forcing=True)
    forced = 0
    both_sent = asyncio.Event()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal forced
        if (json.loads(request.content).get("tool_choice") or {}).get("type") in ("tool", "any"):
            forced += 1
            if forced == 2:
                both_sent.set()
            await asyncio.wait_for(both_sent.wait(), timeout=5)
        return fake.handler(request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    model = providers._anthropic("refuses-forcing-race", None, "test-key", client)  # pyright: ignore[reportPrivateUsage]

    one, two = await asyncio.gather(
        Agent(model, output_type=Verdict).run("is it?"), Agent(model, output_type=Verdict).run("again?")
    )

    assert one.output == two.output == Verdict(answer="yes")
    assert len(fake.choices) == 4, "two forced asks, each retried once"
    assert fake.choices.count("auto") == 2
    assert all(c in ("tool", "any") for c in fake.choices if c != "auto"), "both left forced"


async def test_a_model_that_accepts_forcing_keeps_it() -> None:
    fake = FakeAnthropic(rejects_forcing=False)
    agent = Agent(_model(fake, "accepts-forcing"), output_type=Verdict)

    result = await agent.run("is it?")

    assert result.output == Verdict(answer="yes")
    assert len(fake.choices) == 1 and fake.choices[0] in ("tool", "any")
    assert not providers._FORCED_CHOICE_REFUSED  # pyright: ignore[reportPrivateUsage]


async def test_the_planner_output_parses_on_a_model_that_refuses_forcing() -> None:
    """The planner path that failed live: a `PlanProposal` tool output on a model that refuses
    forcing. Under auto the model may answer in prose first - pydantic-ai asks again (an output
    retry) and the proposal still parses."""
    proposal = {
        "must": ["the work item is submitted"],
        "steps": [
            {
                "label": "s1",
                "agent": "operator",
                "title": "Submit the work item",
                "goal": "Submit the work item in the browser",
            }
        ],
    }
    fake = FakeAnthropic(rejects_forcing=True, args=proposal)
    fake.text_first = True
    agent = Agent(_model(fake, "refuses-forcing-planner"), output_type=PlanProposal, retries=3)

    result = await agent.run("Submit the work item in the machine's browser")

    assert isinstance(result.output, PlanProposal)
    assert [s.agent for s in result.output.steps] == ["operator"]
    assert fake.choices[0] in ("tool", "any") and set(fake.choices[1:]) == {"auto"}
    assert len(fake.choices) == 3, "one refused call, one prose answer retried, one tool call"


# The starter catalog `prismoid models seed` ships (B5): data, so a row may name a model that
# refuses forcing and that pydantic-ai's profiles do not know (Claude 5.5 did).
SEED_CATALOG = Path(providers.__file__).parents[2] / "runner" / "default_models.yaml"
SEEDED_ANTHROPIC = [
    row["model_id"]
    for row in yaml.safe_load(SEED_CATALOG.read_text())["models"]
    if row["protocol"] == Protocol.ANTHROPIC
]


@pytest.mark.parametrize("model_id", SEEDED_ANTHROPIC)
async def test_every_seeded_anthropic_row_serves_a_typed_agent_when_its_model_refuses_forcing(
    model_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built the way every agent gets its model (`build_model`, one catalog row): a typed output
    (the planner's, the judge's) on a seeded row whose model refuses a forced choice ends on its
    output, and the refusal is paid once - the next run asks with auto first."""
    fake = FakeAnthropic(rejects_forcing=True)
    monkeypatch.setattr(throttle, "client_for", lambda provider: fake.client())
    monkeypatch.setattr(providers, "api_key_for", lambda provider: "test-key")

    def build() -> Any:
        return providers.build_model(
            provider="anthropic", model_id=model_id, protocol="anthropic", base_url=None
        )

    first = await Agent(build(), output_type=Verdict).run("is it?")
    assert first.output == Verdict(answer="yes")
    forced = [c for c in fake.choices if c in ("tool", "any")]
    fake.bodies.clear()

    again = await Agent(build(), output_type=Verdict).run("again?")
    assert again.output == Verdict(answer="yes")
    # A row on native structured output (pydantic-ai >=2.52: the 5.5 models) is never forced at all;
    # one on tool output pays the refusal once and then asks with auto.
    assert len(forced) <= 1 and all(c not in ("tool", "any") for c in fake.choices), (
        "a refusing model never sees a forced choice twice"
    )


# The current Anthropic models that reject a forced choice outright (Anthropic's model notes; the
# research note of 2026-11-05). Known up front - by pydantic-ai's model profile - so no process
# pays the 400 first, not even once.
KNOWN_UNFORCED = ["claude-opus-5-5", "claude-sonnet-5-5"]


@pytest.mark.parametrize("model_id", KNOWN_UNFORCED)
async def test_a_model_known_to_refuse_forcing_is_never_forced_even_once(
    model_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeAnthropic(rejects_forcing=True)
    monkeypatch.setattr(throttle, "client_for", lambda provider: fake.client())
    monkeypatch.setattr(providers, "api_key_for", lambda provider: "test-key")
    model = providers.build_model(
        provider="anthropic", model_id=model_id, protocol="anthropic", base_url=None
    )

    for goal in ("is it?", "and again?"):
        assert (await Agent(model, output_type=Verdict).run(goal)).output == Verdict(answer="yes")

    assert len(fake.bodies) == 2, "one request per run: no refused call paid first"
    assert all(c not in ("tool", "any") for c in fake.choices), "no request was ever forced"
