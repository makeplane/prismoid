# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The gateway: a model class in, a metered model out.

No database and no network - the engine is persistence-free, so a catalog is a list of values and
the whole routing layer is testable in microseconds. That is not a happy accident; it is what
Rule 6 buys.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal

import pytest

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import units_of
from prismoid.ai.gateway.resolve import chain_for
from prismoid.errors import ModelMisconfigured, PolicyDenied
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit

QWEN = ModelSpec(
    provider="local",
    model_id="qwen2.5:3b",
    family=Family("qwen"),
    protocol="openai_compatible",
    classes=(ModelClass.FAST_EXECUTOR, ModelClass.CHEAP_CLASSIFIER),
    base_url="http://localhost:11434/v1",
    prices={Unit.INPUT_TOKEN: Decimal("0.0000001"), Unit.OUTPUT_TOKEN: Decimal("0.0000003")},
)
LLAMA = ModelSpec(
    provider="local",
    model_id="llama3.2:3b",
    family=Family("llama"),
    protocol="openai_compatible",
    classes=(ModelClass.FAST_EXECUTOR, ModelClass.JUDGE),
    base_url="http://localhost:11434/v1",
    prices={Unit.INPUT_TOKEN: Decimal("0.00000008"), Unit.OUTPUT_TOKEN: Decimal("0.00000024")},
)
CATALOG = [QWEN, LLAMA]


def test_a_class_resolves_to_every_model_that_serves_it() -> None:
    chain = chain_for(ModelClass.FAST_EXECUTOR, CATALOG)
    assert {m.model_id for m in chain} == {"qwen2.5:3b", "llama3.2:3b"}


def test_a_class_nobody_serves_resolves_to_nothing() -> None:
    assert chain_for(ModelClass.VISION, CATALOG) == []


def test_exclude_family_is_how_cross_family_verification_is_enforced() -> None:
    """The verifier is handed the producer's family and told to be anything else.

    **A model grading its own homework is not verification** - and this is the line that prevents
    it. If this ever returns the producer's own family, every green check in the product becomes
    worthless, and nothing else in the system would notice.
    """
    chain = chain_for(ModelClass.FAST_EXECUTOR, CATALOG, exclude_family=Family("qwen"))
    assert [m.model_id for m in chain] == ["llama3.2:3b"]
    assert all(m.family != Family("qwen") for m in chain)


def test_excluding_the_only_family_leaves_nothing_rather_than_cheating() -> None:
    """Better to fail loudly than to quietly let a model verify itself."""
    only_qwen = [QWEN]
    assert chain_for(ModelClass.CHEAP_CLASSIFIER, only_qwen, exclude_family=Family("qwen")) == []


def test_cost_is_priced_per_native_unit_at_write_time() -> None:
    """The exact sum the live gateway-ping produced: 35 input + 2 output on llama."""
    cost = LLAMA.cost({Unit.INPUT_TOKEN: 35, Unit.OUTPUT_TOKEN: 2, Unit.REQUEST: 1})
    assert cost == Decimal("0.00000328")


def test_an_unpriced_unit_costs_zero_and_that_is_visible() -> None:
    """A call with tokens and no money is exactly the shape of "somebody added a model and forgot
    the price". Silently guessing a price would hide it; costing zero shows it on the ledger."""
    unpriced = ModelSpec(
        provider="x",
        model_id="y",
        family=Family("x"),
        protocol="openai",
        classes=(ModelClass.FAST_EXECUTOR,),
    )
    assert unpriced.cost({Unit.INPUT_TOKEN: 1_000_000}) == Decimal("0")


def test_native_units_are_mapped_never_summed() -> None:
    """Rule 8: no metering, no merge. `cache_read_tokens` are an order of magnitude cheaper than
    `input_tokens` at some providers - a ledger that adds them together cannot tell you why your
    bill halved when you turned caching on."""

    class Usage:
        input_tokens = 1000  # the whole prompt, cached reads included (pydantic-ai's meaning)
        output_tokens = 20
        cache_read_tokens = 900
        cache_write_tokens = 0

    units = units_of(Usage())
    assert units[Unit.INPUT_TOKEN] == 100
    assert units[Unit.OUTPUT_TOKEN] == 20
    assert units[Unit.CACHE_READ_TOKEN] == 900
    assert Unit.CACHE_WRITE_TOKEN not in units  # zero units are absent, not recorded as 0
    assert units[Unit.REQUEST] == 1


def test_an_openai_compatible_model_without_a_base_url_is_refused() -> None:
    """T2's whole promise is `--base-url`. Without one there is nothing to talk to, and the error
    says which flag to type."""
    from prismoid.ai.gateway.providers import build_model

    with pytest.raises(ModelMisconfigured, match="base_url"):
        build_model(provider="x", model_id="y", protocol="openai_compatible", base_url=None)


def test_an_unknown_protocol_names_the_ones_that_exist_and_what_a_new_one_needs() -> None:
    from prismoid.ai.gateway.providers import build_model

    with pytest.raises(ModelMisconfigured, match="novel SDK needs one constructor"):
        build_model(provider="x", model_id="y", protocol="telepathy", base_url=None)


@pytest.mark.parametrize("model_class", [ModelClass.FAST_EXECUTOR, ModelClass.FRONTIER_EXECUTOR])
def test_default_model_settings_set_the_three_cache_breakpoints(model_class: ModelClass) -> None:
    """Instructions + tools + the moving message breakpoint. Anthropic-prefixed
    keys ride every request and are ignored by other protocols - pydantic-ai's own contract -
    so one settings object serves a mixed fallback chain."""
    from prismoid.ai.gateway.providers import default_model_settings

    s = default_model_settings(model_class)
    assert s["anthropic_cache_instructions"] == "1h"
    assert s["anthropic_cache_tool_definitions"] == "1h"
    assert s["anthropic_cache"] is True


def test_an_openai_row_speaks_the_responses_api_with_store_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat Completions refuses function tools on OpenAI's reasoning models
    (`gpt-6-sol` answered 400 "use /v1/responses"), so every OpenAI row - one constructor, no
    per-model case - speaks the Responses API, retains nothing server-side, and carries its
    reasoning across tool turns as encrypted content, the library's stateless path."""
    from pydantic import SecretStr
    from pydantic_ai.models.openai import OpenAIResponsesModel

    from prismoid.ai.gateway.providers import build_model
    from prismoid.conf import settings

    # The OpenAI client refuses to construct without a key; the test must not depend on the
    # machine having one.
    monkeypatch.setattr(settings.worker, "openai_api_key", SecretStr("sk-test"))
    model = build_model(provider="openai", model_id="gpt-6-sol", protocol="openai", base_url=None)
    assert isinstance(model, OpenAIResponsesModel)
    assert (model.settings or {}).get("openai_store") is False
    assert model.profile.get("openai_supports_encrypted_reasoning_content") is True


def test_an_openai_compatible_row_stays_on_chat_completions() -> None:
    """Local OpenAI-compatible servers implement Chat Completions, not Responses."""
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

    from prismoid.ai.gateway.providers import build_model

    model = build_model(
        provider="local",
        model_id="qwen2.5:3b",
        protocol="openai_compatible",
        base_url="http://localhost:11434/v1",
    )
    assert isinstance(model, OpenAIChatModel) and not isinstance(model, OpenAIResponsesModel)
    assert "openai_store" not in (model.settings or {})


async def test_an_openai_request_goes_out_stateless_with_its_reasoning() -> None:
    """On the wire: `store: false`, the encrypted reasoning asked for, and a reasoning item the
    model returned sent back with its encrypted content on the next turn."""
    import json

    import httpx2
    from pydantic_ai.messages import ModelRequest, ModelResponse, ThinkingPart, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    from prismoid.ai.gateway.providers import CONSTRUCTORS
    from prismoid.schemas.model_class import Protocol

    bodies: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 0,
                "model": "gpt-6-sol",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                    }
                ],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 4},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 12,
                },
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    model = CONSTRUCTORS[Protocol.OPENAI]("gpt-6-sol", None, "sk-test", client)
    earlier = ModelResponse(
        parts=[ThinkingPart(content="", id="rs_1", signature="ENC", provider_name="openai")],
        provider_name="openai",
    )
    response = await model.request(
        [ModelRequest(parts=[UserPromptPart("hi")]), earlier, ModelRequest(parts=[UserPromptPart("go on")])],
        None,
        ModelRequestParameters(),
    )
    sent = bodies[0]
    assert sent["store"] is False
    assert "reasoning.encrypted_content" in (sent.get("include") or [])  # pyright: ignore[reportOperatorIssue]
    replayed = [i for i in sent["input"] if isinstance(i, dict) and i.get("type") == "reasoning"]  # pyright: ignore[reportGeneralTypeIssues]
    assert replayed and replayed[0]["encrypted_content"] == "ENC"
    assert (response.usage.input_tokens, response.usage.cache_read_tokens) == (10, 4)


def test_cached_input_is_metered_once_at_its_own_rate() -> None:
    """pydantic-ai's `input_tokens` is the WHOLE prompt - cached reads and cache writes included
    (both providers, checked against the library's own extraction) - so the uncached remainder is
    what the input rate bills. Mapping the total as well billed every cached token twice."""
    from pydantic_ai.usage import RequestUsage

    anthropic = RequestUsage.extract(
        {
            "model": "claude-sonnet-4-5",
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 4775,
                "cache_creation_input_tokens": 326,
                "output_tokens": 142,
            },
        },
        provider="anthropic",
        provider_url="https://api.anthropic.com",
        provider_fallback="anthropic",
    )
    assert units_of(anthropic) == {
        Unit.INPUT_TOKEN: 2,
        Unit.CACHE_READ_TOKEN: 4775,
        Unit.CACHE_WRITE_TOKEN: 326,
        Unit.OUTPUT_TOKEN: 142,
        Unit.REQUEST: 1,
    }
    openai = RequestUsage.extract(
        {
            "model": "gpt-5",
            "usage": {
                "input_tokens": 433,
                "input_tokens_details": {"cached_tokens": 200},
                "output_tokens": 154,
            },
        },
        provider="openai",
        provider_url="https://api.openai.com",
        provider_fallback="openai",
        api_flavor="responses",
    )
    assert units_of(openai) == {
        Unit.INPUT_TOKEN: 233,
        Unit.CACHE_READ_TOKEN: 200,
        Unit.OUTPUT_TOKEN: 154,
        Unit.REQUEST: 1,
    }


def _row(provider: str, model_id: str, *classes: ModelClass) -> ModelSpec:
    return ModelSpec(
        provider=provider,
        model_id=model_id,
        family=Family(provider),
        protocol=provider,
        classes=classes or (ModelClass.FAST_EXECUTOR,),
    )


def _keys(monkeypatch: pytest.MonkeyPatch, **keys: str) -> None:
    """Hermetic: only the keys a test names exist - never the developer's `.env`, never the
    process env a provider SDK would otherwise fall back to."""
    from prismoid.ai.gateway import providers

    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(providers, "api_key_for", lambda provider: keys.get(provider, ""))


def test_build_model_turns_every_refusal_to_construct_into_one_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key, or the SDK refusing to construct (OpenAI's `OpenAIError`, pydantic-ai's `UserError`
    for Google): one error, wearing the row's ref, for `resolve` to skip on."""
    from prismoid.ai.gateway.providers import build_model
    from prismoid.errors import ProviderUnavailable

    _keys(monkeypatch)
    for provider, model_id in (
        ("openai", "gpt-6-sol"),
        ("google", "gemini-flash-latest"),
        ("anthropic", "claude-sonnet-5"),
    ):
        with pytest.raises(ProviderUnavailable) as refused:
            build_model(provider=provider, model_id=model_id, protocol=provider, base_url=None)
        assert refused.value.context["ref"] == f"{provider}:{model_id}"
    # a local OpenAI-compatible server needs no key at all
    build_model(provider="local", model_id="qwen2.5:3b", protocol="openai_compatible", base_url="http://x/v1")


def test_a_provider_that_cannot_be_built_here_costs_a_candidate_not_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`models seed` enables Google beside Anthropic, and on a host with no Google key the Google
    provider refuses to be constructed. That refusal raised out of `resolve` and failed every step
    whose chain held a Google row, the Anthropic candidates included. One family down must cost
    latency, never the answer: the chain is built from the candidates that CAN be built."""
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    from prismoid.ai.gateway.meter import MeteredModel
    from prismoid.ai.gateway.resolve import resolve

    _keys(monkeypatch, anthropic="k")
    anthropic, google = _row("anthropic", "claude-haiku-4-5"), _row("google", "gemini-flash-latest")

    model = resolve(ModelClass.FAST_EXECUTOR, [google, anthropic], order={"fast-executor": [google.ref]})
    assert isinstance(model, ConcurrencyLimitedModel)
    assert isinstance(model.wrapped, MeteredModel) and model.wrapped.spec == anthropic

    with pytest.raises(PolicyDenied, match="google:gemini-flash-latest"):
        resolve(ModelClass.FAST_EXECUTOR, [google])


def test_a_host_with_no_frontier_executor_it_can_build_runs_the_lead_on_fast_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Google-only host, or a catalog not re-seeded since the class shipped: the Lead keeps the class it
    had before, never every turn failing."""
    from prismoid.ai.gateway.resolve import head_row, resolve

    _keys(monkeypatch, google="k")
    sonnet = _row("anthropic", "claude-sonnet-5-5", ModelClass.FAST_EXECUTOR, ModelClass.FRONTIER_EXECUTOR)
    gemini = _row("google", "gemini-3.8-flash")
    assert head_row(resolve(ModelClass.FRONTIER_EXECUTOR, [sonnet, gemini])) == gemini
    assert head_row(resolve(ModelClass.FRONTIER_EXECUTOR, [gemini])) == gemini  # no row at all


def test_a_misconfigured_row_costs_its_seat_and_is_not_a_policy_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row nobody could build (here an OpenAI-compatible server with no base URL) is the
    operator's data, not a refusal: building it raises `ModelMisconfigured`, and `resolve` skips it
    like a keyless row, so the next row serves."""
    from dataclasses import replace

    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    from prismoid.ai.gateway.meter import MeteredModel
    from prismoid.ai.gateway.providers import build_model
    from prismoid.ai.gateway.resolve import resolve

    _keys(monkeypatch, anthropic="k")
    anthropic = _row("anthropic", "claude-haiku-4-5")
    local = replace(_row("local", "llama-9"), protocol="openai_compatible", base_url=None)

    with pytest.raises(ModelMisconfigured) as refused:
        build_model(provider="local", model_id="llama-9", protocol="openai_compatible", base_url=None)
    assert not isinstance(refused.value, PolicyDenied)

    model = resolve(ModelClass.FAST_EXECUTOR, [local, anthropic], order={"fast-executor": [local.ref]})
    assert isinstance(model, ConcurrencyLimitedModel)
    assert isinstance(model.wrapped, MeteredModel) and model.wrapped.spec == anthropic


def test_a_keyless_openai_failover_costs_its_seat_and_the_first_seat_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found on the stack: the seeded computer-use chain is [Sonnet, gpt-6-sol], and on a host with
    no OpenAI key building `gpt-6-sol` raised `OpenAIError` and failed every computer-use step
    before any call, Sonnet included. The keyless row is skipped: the chain is the rows that can be
    built."""
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    from prismoid.ai.gateway.meter import MeteredModel
    from prismoid.ai.gateway.resolve import resolve, served_here

    _keys(monkeypatch, anthropic="k")
    use = ModelClass.COMPUTER_USE
    sonnet, gpt = _row("anthropic", "claude-sonnet-5", use), _row("openai", "gpt-6-sol", use)
    order = {use.value: [sonnet.ref, gpt.ref]}

    model = resolve(use, [sonnet, gpt], order=order)
    assert isinstance(model, ConcurrencyLimitedModel)
    assert isinstance(model.wrapped, MeteredModel) and model.wrapped.spec == sonnet
    assert served_here(use, [sonnet, gpt]) and not served_here(use, [gpt])


def test_a_fleet_seats_only_executor_rows_this_host_can_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Found live: a keyless Google row held a fleet seat and failed all three tries before any call."""
    from prismoid.ai.gateway.resolve import seatable

    _keys(monkeypatch, anthropic="k")
    sonnet, gemini = _row("anthropic", "claude-sonnet-5"), _row("google", "gemini-3.8-flash")
    judge = _row("anthropic", "claude-judge", ModelClass.JUDGE)
    assert seatable([sonnet, gemini, judge]) == [sonnet]


def test_when_no_row_can_be_built_the_step_fails_naming_each(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.gateway.resolve import resolve

    _keys(monkeypatch)
    use = ModelClass.COMPUTER_USE
    sonnet, gpt = _row("anthropic", "claude-sonnet-5", use), _row("openai", "gpt-6-sol", use)
    with pytest.raises(PolicyDenied, match="computer-use") as refused:
        resolve(use, [sonnet, gpt], order={use.value: [sonnet.ref, gpt.ref]})
    assert "anthropic:claude-sonnet-5" in str(refused.value) and "openai:gpt-6-sol" in str(refused.value)


# ── provider keys (§9 T2) ────────────────────────────────────────────────────────────────────
#
# A new OpenAI-compatible provider is "an env key + `--base-url`, no code". The key's home on a
# dev box is `.env` - which pydantic-settings reads but never exports into `os.environ`, so a
# Baseten key written there reached nothing and the row built keyless (found 2026-10-24).


def test_a_dotenv_only_provider_key_reaches_the_gateway(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.ai.gateway import providers

    (tmp_path / ".env").write_text(
        "PRISMOID_GATEWAY_BASETEN_API_KEY=bk-from-dotenv\nPRISMOID_WORKER_ANTHROPIC_API_KEY=another-groups-secret\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRISMOID_GATEWAY_BASETEN_API_KEY", raising=False)

    assert providers.api_key_for("baseten") == "bk-from-dotenv"
    assert providers.api_key_for("nobody") == ""
    # The convention names one group's key: no provider name reaches another group's secret.
    assert providers.api_key_for("worker_anthropic") == ""


def test_an_exported_provider_key_outranks_dotenv(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.ai.gateway import providers

    (tmp_path / ".env").write_text("PRISMOID_GATEWAY_BASETEN_API_KEY=bk-from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRISMOID_GATEWAY_BASETEN_API_KEY", "bk-exported")
    assert providers.api_key_for("baseten") == "bk-exported"


# ── streamed usage: a cumulative report is counted once (found live 2026-10-25) ───────────────
#
# Baseten sends the cumulative total on EVERY chunk; pydantic-ai adds chunks up. A 14-second
# GLM-5.3-Flash memo was metered at 1.92M input tokens. The increment rule fixes it for every
# server shape; the streaming test pins the private hook it rides (ADR-006 entry 006).


def test_usage_increment_counts_a_cumulative_report_once() -> None:
    from pydantic_ai.usage import RequestUsage

    from prismoid.ai.gateway.providers import usage_increment

    first = usage_increment(RequestUsage(input_tokens=19, output_tokens=1), RequestUsage())
    assert (first.input_tokens, first.output_tokens) == (19, 1), "the first report is taken whole"
    counted = RequestUsage(input_tokens=19, output_tokens=1)
    later = usage_increment(RequestUsage(input_tokens=19, output_tokens=25), counted)
    assert (later.input_tokens, later.output_tokens) == (0, 24), "a cumulative report adds its increase"
    empty = usage_increment(RequestUsage(), counted)
    assert (empty.input_tokens, empty.output_tokens) == (0, 0), "a chunk with no usage adds nothing"
    smaller = usage_increment(RequestUsage(input_tokens=0, output_tokens=3), counted)
    assert smaller.output_tokens == 3, "a smaller report is an increment, never dropped"


def _sse(chunks: list[dict[str, object]]) -> bytes:
    import json

    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _chunk(text: str, usage: tuple[int, int] | None, *, finish: str | None = None) -> dict[str, object]:
    chunk: dict[str, object] = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "zai-org/GLM-5.3-Flash",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": finish}],
    }
    if usage is not None:
        chunk["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        }
    return chunk


@pytest.mark.parametrize(
    "chunks",
    [
        # Baseten: the cumulative total on every chunk.
        [_chunk("Hel", (19, 1)), _chunk("lo th", (19, 9)), _chunk("ere.", (19, 25), finish="stop")],
        # OpenAI's shape: usage once, at the end.
        [_chunk("Hel", None), _chunk("lo th", None), _chunk("ere.", (19, 25), finish="stop")],
    ],
    ids=["cumulative-every-chunk", "final-chunk-only"],
)
async def test_a_streamed_openai_compatible_call_is_metered_once(chunks: list[dict[str, object]]) -> None:
    import httpx2  # the provider wire's client (ADR-072)
    from pydantic_ai import Agent

    from prismoid.ai.gateway.providers import CONSTRUCTORS
    from prismoid.schemas.model_class import Protocol

    body = _sse(chunks)
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})
        )
    )
    model = CONSTRUCTORS[Protocol.OPENAI_COMPATIBLE](
        "zai-org/GLM-5.3-Flash", "http://baseten.test/v1", "k", client
    )
    async with Agent(model).run_stream("hi") as run:
        assert await run.get_output() == "Hello there."
        usage = run.usage
    assert (usage.input_tokens, usage.output_tokens) == (19, 25)
