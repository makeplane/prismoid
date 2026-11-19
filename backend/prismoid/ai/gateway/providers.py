# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Model constructors, one per wire protocol and never per model: the model id arrives as data from a catalog row.

A provider that is not a chat model (speech, images, transcription) is a tool under `ai/tools/providers/`."""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any, Final, cast, get_args

import httpx2
import logfire
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ServiceTier, ThinkingEffort

from prismoid.ai import config
from prismoid.ai.gateway import throttle
from prismoid.errors import ModelMisconfigured, ProviderUnavailable
from prismoid.schemas.model_class import ModelClass, Protocol

if TYPE_CHECKING:
    from anthropic.types.beta import BetaContextManagementConfigParam
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import RequestUsage

# The settings key that routes one task's agent to the OpenAI server holding its cached prefix.
CACHE_KEY_SETTING: Final = "openai_prompt_cache_key"
# The settings that ask for Anthropic's 1-hour cache tier (`default_model_settings`).
HOUR_TTL_SETTINGS: Final = frozenset({"anthropic_cache_instructions", "anthropic_cache_tool_definitions"})
# A request's idle timeout for a call with no class, or a class with no entry in `CLASS_TIMEOUT_S`.
DEFAULT_TIMEOUT_S: Final = 300.0

# Model ids that refused a forced `tool_choice` in this process: such a model answers 400 to every agent
# with a structured output (the Lead first), and pydantic-ai knows only some of them by name. Process
# state, not a row (throttle.py's precedent): the provider re-teaches it after a restart in one call.
_FORCED_CHOICE_REFUSED: set[str] = set()


# The forced types themselves, quoted, as the provider names them: `type "tool" and "any"`.
_FORCED_TYPE_RE = re.compile(r"""["'](?:tool|any)["']""")


def _refuses_forced_choice(exc: BaseException) -> bool:
    """The provider's own words for "this model rejects a forced tool_choice" - a 400 whose
    message names `tool_choice`, a forced type (`"tool"`/`"any"`) and "not supported". Narrow on
    purpose: any other 400, a tool_choice complaint about another type included, is a fact
    about the request and passes through untouched."""
    from pydantic_ai.exceptions import ModelHTTPError

    if not isinstance(exc, ModelHTTPError) or exc.status_code != 400:
        return False
    body = exc.body
    error = cast("dict[str, Any]", body).get("error") if isinstance(body, dict) else None
    message = cast("dict[str, Any]", error).get("message") if isinstance(error, dict) else None
    said = (message if isinstance(message, str) else str(exc.body)).lower()
    return "tool_choice" in said and "not supported" in said and bool(_FORCED_TYPE_RE.search(said))


def _unforced(model_id: str, provider: AnthropicProvider) -> Model:
    """The same model with forcing off: pydantic-ai then sends `tool_choice: auto` (filtering the
    tools to the requested ones) and an output tool rides an ordinary output retry."""
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.profiles import ModelProfile

    return AnthropicModel(
        model_id,
        provider=provider,
        profile=ModelProfile(supports_forced_tool_choice=False),
    )


class _ForcedChoiceFallback(WrapperModel):
    """An Anthropic model that heals itself the first time the provider refuses a forced
    `tool_choice`: it remembers the model id, swaps to the unforced build and retries THAT
    request once - on the request and the stream path alike. A healed id is built unforced from
    the start (`_anthropic`), and an instance built before another one healed swaps before it
    asks, so the 400 is paid once per model per process."""

    def __init__(self, model_id: str, provider: AnthropicProvider) -> None:
        from pydantic_ai.models.anthropic import AnthropicModel

        super().__init__(AnthropicModel(model_id, provider=provider))
        self._model_id = model_id
        self._anthropic_provider = provider
        self._healed = False

    def _heal(self, *, refused: bool = False) -> None:
        """Swap to the unforced build when this id is known to refuse forcing (or just did)."""
        if refused and self._model_id not in _FORCED_CHOICE_REFUSED:
            _FORCED_CHOICE_REFUSED.add(self._model_id)
            logfire.warn(
                "gateway: {model} refuses a forced tool_choice; asking with tool_choice auto from now on",
                model=self._model_id,
            )
        if not self._healed and self._model_id in _FORCED_CHOICE_REFUSED:
            self.wrapped = _unforced(self._model_id, self._anthropic_provider)
            self._healed = True

    def _retry_after(self, exc: Exception, sent: Model) -> bool:
        """True when `exc` is the refusal and the model now differs from the one that `sent` the
        request - a concurrent request on this instance may have healed it already, and this one
        still deserves its retry. A refusal from the unforced build itself is never retried."""
        if not _refuses_forced_choice(exc):
            return False
        self._heal(refused=True)
        return self.wrapped is not sent

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._heal()
        sent = self.wrapped
        try:
            return await sent.request(messages, model_settings, model_request_parameters)
        except Exception as exc:
            if not self._retry_after(exc, sent):
                raise
        return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncGenerator[StreamedResponse]:
        self._heal()
        sent = self.wrapped
        async with AsyncExitStack() as stack:
            try:
                stream = await stack.enter_async_context(
                    sent.request_stream(messages, model_settings, model_request_parameters, run_context)
                )
            except Exception as exc:
                if not self._retry_after(exc, sent):
                    raise
                stream = await stack.enter_async_context(
                    self.wrapped.request_stream(
                        messages, model_settings, model_request_parameters, run_context
                    )
                )
            yield stream


def _anthropic(model_id: str, base_url: str | None, api_key: str, http_client: httpx2.AsyncClient) -> Model:
    from anthropic import AsyncAnthropic
    from pydantic_ai.providers.anthropic import AnthropicProvider

    # The tenacity transport (throttle.py) is the one retrier; the SDK's own default budget (2)
    # would stack on top of it → ~9 upstream calls.
    client = AsyncAnthropic(api_key=api_key, http_client=http_client, max_retries=throttle.SDK_MAX_RETRIES)
    provider = AnthropicProvider(anthropic_client=client)
    if model_id in _FORCED_CHOICE_REFUSED:
        return _unforced(model_id, provider)  # healed earlier in this process: no second 400
    return _ForcedChoiceFallback(model_id, provider)


def _openai(model_id: str, base_url: str | None, api_key: str, http_client: httpx2.AsyncClient) -> Model:
    """OpenAI's own rows speak the **Responses API**, every one of them. Chat Completions refuses
    function tools on OpenAI's reasoning models (`gpt-6-sol` answered 400, "use
    /v1/responses"), so a tool-using agent could not run on such a row at all.

    `openai_store=False`: OpenAI retains nothing of a request server-side. The model's reasoning
    then rides the message history instead, as encrypted content - pydantic-ai's stateless path,
    keyed on the profile flag set here: ask for `reasoning.encrypted_content`, send each reasoning
    item back with it on the next tool turn. Set for every row rather than read from the
    library's model table (which lags new families); a row that does not reason returns no
    reasoning, and the ask costs nothing. (OpenAI-compatible servers stay on Chat Completions:
    they do not implement Responses - `_openai_compatible`.)"""
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
    from pydantic_ai.profiles import merge_profile
    from pydantic_ai.profiles.openai import OpenAIModelProfile
    from pydantic_ai.providers.openai import OpenAIProvider

    client = AsyncOpenAI(api_key=api_key, http_client=http_client, max_retries=throttle.SDK_MAX_RETRIES)
    provider = OpenAIProvider(openai_client=client)
    profile = merge_profile(
        provider.model_profile(model_id), OpenAIModelProfile(openai_supports_encrypted_reasoning_content=True)
    )
    return OpenAIResponsesModel(
        model_id,
        provider=provider,
        profile=profile,
        settings=OpenAIResponsesModelSettings(openai_store=False),
    )


def _google(model_id: str, base_url: str | None, api_key: str, http_client: httpx2.AsyncClient) -> Model:
    from google.genai.types import HttpRetryOptions
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    # google-genai counts attempts, the first included; pinning it keeps the transport the one
    # retrier if its default ever changes. api_key+http_client stay for its own timeout wiring.
    retries = HttpRetryOptions(attempts=throttle.SDK_MAX_RETRIES + 1)
    return GoogleModel(
        model_id,
        provider=GoogleProvider(api_key=api_key, http_client=http_client, retry_options=retries),
    )


def _openai_compatible(
    model_id: str, base_url: str | None, api_key: str, http_client: httpx2.AsyncClient
) -> Model:
    """DeepSeek · xAI · Ollama · vLLM · LM Studio · most startups · whatever ships next week.

    A new provider that speaks the OpenAI protocol needs no code: it is served by pointing `base_url` at it - no adapter, no branch, no release. The catalog row
    carries the URL; the key comes from `PRISMOID_GATEWAY_<NAME>_API_KEY`; and a model running on
    your laptop is the same kind of thing as one running in Virginia.

    **Structured output goes through `json_schema`, not through function calling - and that is a
    protocol fact, not a preference.** An OpenAI-compatible server's weakest surface is its tool
    calling: pydantic-ai's default is `tool` mode, and under it *every* local model this project has
    tried - llama3.2:3b, qwen2.5:3b, and even qwen2.5:7b on a one-sentence prompt - fails with
    `Exceeded maximum output retries`. Under `native` mode (the server's own `response_format:
    json_schema`, which each of these profiles already advertises) the same 7B answers correctly.
    """
    from openai import AsyncOpenAI
    from pydantic_ai.profiles import ModelProfile
    from pydantic_ai.providers.openai import OpenAIProvider

    if not base_url:
        raise ModelMisconfigured(
            f"model {model_id!r} declares the openai_compatible protocol but has no base_url - "
            "add one with `prismoid models add … --base-url URL`",
        )

    # Local servers (Ollama, vLLM) usually want no key at all; the SDK still insists on a string.
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or "not-needed",
        http_client=http_client,
        max_retries=throttle.SDK_MAX_RETRIES,
    )
    provider = OpenAIProvider(openai_client=client)

    # Keep everything pydantic-ai already knows about this model, and change the **one** thing it
    # has wrong for a server that is not OpenAI. Merging rather than replacing matters: the resolved
    # profile carries the model's real token limits, reasoning support and tool quirks, and throwing
    # all of that away to fix one field would trade a known bug for several unknown ones.
    known = provider.model_profile(model_id) or ModelProfile()
    profile = ModelProfile({**known, "default_structured_output_mode": "native"})

    return _compatible_chat_model()(model_id, provider=provider, profile=profile)


def usage_increment(seen: RequestUsage, counted: RequestUsage) -> RequestUsage:
    """What one streamed chunk's usage ADDS to what the stream has counted.

    OpenAI-compatible servers disagree on what a chunk's `usage` means. OpenAI and most servers
    send it once, on the last chunk; Baseten (and vLLM's continuous stats) send the CUMULATIVE
    total on every chunk - and pydantic-ai adds every chunk's usage up, an over-count that grows with
    the square of the reply's length.

    So a report that is >= what was counted in every field, after something was counted, is
    cumulative and contributes only its increase; anything else - the first report, a chunk with
    no usage (all zeros), a smaller report - is added whole, exactly as before. Right for every
    server shape, and it never under-counts a server that does report increments."""
    from pydantic_ai.usage import RequestUsage

    ints = [f.name for f in dataclasses.fields(seen) if f.name not in ("details", "cost")]
    started = any(getattr(counted, name) for name in ints)
    if not started or not all(getattr(seen, name) >= getattr(counted, name) for name in ints):
        return seen
    return RequestUsage(
        **{name: getattr(seen, name) - getattr(counted, name) for name in ints},
        details={key: value - counted.details.get(key, 0) for key, value in seen.details.items()},
        cost=seen.cost - counted.cost if seen.cost is not None and counted.cost is not None else seen.cost,
    )


_FIRST_PARTY_ONLY: Final = frozenset({"service_tier", CACHE_KEY_SETTING})


@functools.cache
def _compatible_chat_model() -> type[OpenAIChatModel]:
    """`OpenAIChatModel` whose stream meters by `usage_increment`. `_streamed_response_cls` is the
    library's documented override point; the stream's `_map_usage` is private (ADR-006 entry 006,
    guarded by `test_gateway`'s streaming test). Built once: the import is lazy, like every
    constructor here."""
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse

    class _IncrementalUsageStream(OpenAIStreamedResponse):
        def _map_usage(self, response: Any) -> RequestUsage:
            return usage_increment(super()._map_usage(response), self._usage)

    class _CompatibleChatModel(OpenAIChatModel):
        @property
        def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
            return _IncrementalUsageStream

        def prepare_request(
            self, model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
        ) -> tuple[ModelSettings | None, ModelRequestParameters]:
            """The shared settings minus the service tier and the cache key: both name a first-party API's
            capacity, which a compatible server does not have, and Chat Completions would send them verbatim."""
            settings, parameters = super().prepare_request(model_settings, model_request_parameters)
            if settings is not None and _FIRST_PARTY_ONLY & settings.keys():
                settings = cast(
                    "ModelSettings", {k: v for k, v in settings.items() if k not in _FIRST_PARTY_ONLY}
                )
            return settings, parameters

    return _CompatibleChatModel


CONSTRUCTORS = {
    Protocol.ANTHROPIC: _anthropic,
    Protocol.OPENAI: _openai,
    Protocol.GOOGLE: _google,
    Protocol.OPENAI_COMPATIBLE: _openai_compatible,
}


@dataclasses.dataclass(frozen=True, slots=True)
class Features:
    """What one wire protocol can do. Every protocol check in the product reads this table."""

    chat: bool  # a chat `Model` is built for it; a media protocol is served by the media tool
    keyed: bool  # its SDK cannot serve a call without the provider's key
    clears_history: bool  # its server can trim old tool results itself (`CONTEXT_EDITS`)


FEATURES: Final[dict[Protocol, Features]] = {
    Protocol.ANTHROPIC: Features(chat=True, keyed=True, clears_history=True),
    Protocol.OPENAI: Features(chat=True, keyed=True, clears_history=False),
    Protocol.GOOGLE: Features(chat=True, keyed=True, clears_history=False),
    Protocol.OPENAI_COMPATIBLE: Features(chat=True, keyed=False, clears_history=False),
    Protocol.OPENAI_IMAGES: Features(chat=False, keyed=True, clears_history=False),
    Protocol.GOOGLE_IMAGES: Features(chat=False, keyed=True, clears_history=False),
}


def features(protocol: str) -> Features:
    """The features of a catalog row's protocol; an unknown protocol raises `ValueError`."""
    return FEATURES[Protocol(protocol)]


def server_clears(protocol: str, model_class: ModelClass) -> bool:
    """Whether a row on `protocol` serving `model_class` has its old tool results cleared by its server."""
    return features(protocol).clears_history and model_class in SERVER_CLEARED_CLASSES


# A request timeout per model class. It is an IDLE bound, not a wall clock: one number reaches httpx2 as its
# connect/read/write/pool timeouts, so a request dies after this long with no byte from the
# provider, but a stream that keeps sending (slow tokens, keep-alives) runs on to the step's wall
# clock. A failed fallback chain costs up to this × its length (`resolve.CHAIN_CAP`); each hop
# logs its row, reason and duration (`meter.failure_reason`). The numbers follow the work:
# reasoning/long-context/code calls legitimately go quiet for minutes; classifiers and judges
# that go quiet for 90 s are stuck, not thorough.
CLASS_TIMEOUT_S: dict[ModelClass, float] = {
    ModelClass.FRONTIER_REASONER: 300.0,
    ModelClass.LONG_CONTEXT: 300.0,
    ModelClass.CODE: 300.0,
    ModelClass.FAST_EXECUTOR: 180.0,
    ModelClass.FRONTIER_EXECUTOR: 180.0,
    ModelClass.VISION: 180.0,
    ModelClass.CHEAP_CLASSIFIER: 90.0,
    ModelClass.DECIDER: 90.0,
    ModelClass.JUDGE: 90.0,
    ModelClass.COMPUTER_USE: 180.0,
}

# The output ceiling, set on purpose: providers default it low (Anthropic: 4,096), which cuts a full
# document short. 16k is above any sane deliverable, below every catalog model's output limit, and
# under the size providers refuse without a stream (judges and the Lead call without one).
DEFAULT_MAX_TOKENS = 16_000

# Server-side tool-result clearing (Anthropic context editing), for computer use only: a screenshot
# loop is the one run whose old results are worthless, and everywhere else an early read (a maker's
# format skill, a researcher's pages) is what the work rests on. Once the prompt passes `trigger` the
# server clears the oldest tool results, keeping the newest `keep` calls, and only when at least
# `clear_at_least` goes: every clear re-writes the cached prefix once, so it must come in large, rare
# batches. The client never trims a history the server clears - a client edit to an earlier turn
# invalidates every later thinking block - and a server-side clear is not an edit; every other class
# is bounded on the client (`capabilities/compaction.py`). pydantic-ai adds only the compaction beta
# for this setting; the clearing beta is named here.
SERVER_CLEARED_CLASSES: Final = frozenset({ModelClass.COMPUTER_USE})
CONTEXT_EDITS: BetaContextManagementConfigParam = {
    "edits": [
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": 40_000},
            "keep": {"type": "tool_uses", "value": 6},
            "clear_at_least": {"type": "input_tokens", "value": 15_000},
        }
    ]
}
_CONTEXT_EDITING_BETA = "context-management-2025-06-27"


def warm_sdks() -> list[str]:
    """Import the chat SDKs `CONSTRUCTORS` import lazily, once, at worker boot - never on a task's
    path. The lazy imports keep the API process lean; in the worker the first `build_model` paid
    them inside the first step (1.2 s measured on the dev stack), so the first talk turn after
    every restart missed first-delta <2 s. Best-effort: an SDK this host does not install is
    skipped (its row refuses at build, as before). Returns the modules that loaded."""
    import importlib

    loaded: list[str] = []
    for name in (
        "anthropic",
        "openai",
        "pydantic_ai.models.anthropic",
        "pydantic_ai.providers.anthropic",
        "pydantic_ai.models.openai",
        "pydantic_ai.providers.openai",
    ):
        try:
            importlib.import_module(name)
        except ImportError:
            continue
        loaded.append(name)
    return loaded


@dataclasses.dataclass(frozen=True, slots=True)
class ClassSettings:
    """How one model class is asked, from its routing-table row's `settings`: the reasoning effort and
    the service tier, both pydantic-ai's unified fields, which each protocol's model maps onto its wire."""

    effort: ThinkingEffort | None = None
    service_tier: ServiceTier | None = None

    @classmethod
    def read(cls, raw: Mapping[str, object], *, model_class: str) -> ClassSettings:
        """The row's settings as a value: an unknown key, or a value the framework does not name, is
        dropped with a warning and never sent (fail-soft: a bad row costs the setting, never the class)."""
        for key in raw.keys() - _SETTING_KEYS:
            logfire.warn("gateway: {cls} settings drop unknown key {key!r}", cls=model_class, key=key)
        return cls(
            effort=_named(raw, "effort", get_args(ThinkingEffort), model_class),
            service_tier=_named(raw, "service_tier", get_args(ServiceTier), model_class),
        )


_SETTING_KEYS: Final = frozenset(f.name for f in dataclasses.fields(ClassSettings))


def _named[T: str](raw: Mapping[str, object], key: str, allowed: tuple[T, ...], model_class: str) -> T | None:
    """`raw[key]` when it is one of `allowed`; None when absent, or (with a warning) anything else."""
    value = raw.get(key)
    if value is None:
        return None
    for name in allowed:
        if value == name:
            return name
    logfire.warn(
        "gateway: {cls} settings drop {key}={value!r}, not one of {allowed}",
        cls=model_class,
        key=key,
        value=value,
        allowed=list(allowed),
    )
    return None


# Anthropic bills cached prefix reads at ~0.1x, and an agent's instructions and toolset are
# byte-stable while a tool loop re-sends the conversation every round, so three breakpoints:
# instructions and tool definitions on the 1-hour TTL Anthropic prices (write 2x) for the classes
# a person's next turn re-reads, because that often comes after five minutes; a one-shot class (a
# title, a decision, a verdict) is never re-read after its run, so it stays on 5 minutes (write
# 1.25x), as does the moving message breakpoint, which a tool loop re-reads within seconds.
# OpenAI has no 1-hour tier and caches implicitly; it is sent only `CACHE_KEY_SETTING`, which
# routes one task's agent to the server holding its prefix. Provider-prefixed keys are ignored by
# every other protocol (pydantic-ai's convention), so one settings object serves a fallback chain
# that mixes providers. The class's effort and tier ride the unified `thinking` and `service_tier`
# fields: each model maps them per protocol, and a model whose profile takes no effort is sent none.
HOUR_CACHED_CLASSES: Final = frozenset(
    {
        ModelClass.FAST_EXECUTOR,
        ModelClass.FRONTIER_EXECUTOR,
        ModelClass.FRONTIER_REASONER,
        ModelClass.LONG_CONTEXT,
        ModelClass.CODE,
        ModelClass.VISION,
        ModelClass.COMPUTER_USE,
    }
)


def default_model_settings(
    model_class: ModelClass | None = None,
    settings: ClassSettings | None = None,
    *,
    cache_key: str | None = None,
) -> Any:
    """One settings object for the whole chain: output ceiling, request timeout, prompt-cache
    breakpoints and routing key, computer use's tool-result clearing, and the class's effort and tier."""
    from pydantic_ai.models.anthropic import AnthropicModelSettings

    static = "1h" if model_class in HOUR_CACHED_CLASSES else "5m"
    out = AnthropicModelSettings(
        max_tokens=DEFAULT_MAX_TOKENS,
        timeout=CLASS_TIMEOUT_S.get(model_class, DEFAULT_TIMEOUT_S)
        if model_class is not None
        else DEFAULT_TIMEOUT_S,
        anthropic_cache_instructions=static,
        anthropic_cache_tool_definitions=static,
        anthropic_cache=True,
    )
    if model_class in SERVER_CLEARED_CLASSES:
        out["anthropic_context_management"] = CONTEXT_EDITS
        out["anthropic_betas"] = [_CONTEXT_EDITING_BETA]
    if settings is not None and settings.effort is not None:
        out["thinking"] = settings.effort
    if settings is not None and settings.service_tier is not None:
        out["service_tier"] = settings.service_tier
    if cache_key is None:
        return out
    return {**out, CACHE_KEY_SETTING: cache_key}


def api_key_for(provider: str) -> str:
    """Keys are conf, never catalog. A key in a database row is a key in a database backup.

    First-party providers have named settings; everything else follows one convention -
    `PRISMOID_GATEWAY_<PROVIDER>_API_KEY` - so a *new* provider needs no code here either.
    """
    cfg = config.current()
    first_party = {
        "anthropic": cfg.worker.anthropic_api_key,
        "openai": cfg.worker.openai_api_key,
        "google": cfg.worker.google_api_key,
        "deepseek": cfg.gateway.deepseek_api_key,
        "xai": cfg.gateway.xai_api_key,
    }
    if provider in first_party:
        return first_party[provider].get_secret_value()

    import os

    from pydantic_settings import DotEnvSettingsSource

    # The process environment first (a compose `env_file`, an exported shell var), then `.env` -
    # the precedence pydantic-settings gives every named field. pydantic-settings never exports
    # `.env` into `os.environ`, so a `.env`-only key used to reach nothing and the row built
    # keyless; the dotenv source conf itself uses reads it here (lowercased names).
    name = f"PRISMOID_GATEWAY_{provider.upper()}_API_KEY"
    return (
        os.environ.get(name) or DotEnvSettingsSource(config.GatewaySettings).env_vars.get(name.lower()) or ""
    )


def build_model(*, provider: str, model_id: str, protocol: str, base_url: str | None) -> Model:
    """One model, from one catalog row. The only place a `Model` object is constructed.

    The client comes from `throttle.client_for(provider)` - shared, keep-alive, retrying - so a
    forty-row catalog resolves without forty TLS handshakes, and a 429 pauses the same model
    instead of silently switching authors (see `throttle`)."""
    try:
        wire = Protocol(protocol)
    except ValueError:
        raise ModelMisconfigured(
            f"unknown protocol {protocol!r}. Known: {[p.value for p in Protocol]}. "
            "A genuinely novel SDK needs one constructor here, a dependency row and an ADR.",
            ref=f"{provider}:{model_id}",
        ) from None
    if not FEATURES[wire].chat:
        raise ModelMisconfigured(
            f"{provider}:{model_id} speaks {protocol!r} - a MEDIA protocol, served by the media "
            "lane (`ai/tools/providers/images.py`, ADR-024), never constructed as a chat model. "
            "An image model cannot hold a fleet seat or serve a chat class.",
            ref=f"{provider}:{model_id}",
        )
    from anthropic import AnthropicError
    from openai import OpenAIError
    from pydantic_ai.exceptions import UserError

    # **A row this host cannot build is refused here, one way for every provider** - no key for
    # a first-party SDK that needs one (Anthropic's would build and 401 on every call), or the
    # SDK refusing to construct (OpenAI's `OpenAIError`, pydantic-ai's `UserError` for Google).
    # `resolve` skips it: `models seed` enables every family it ships, and a host holds the keys
    # it holds. An OpenAI-compatible server may need no key at all (a local Ollama).
    ref = f"{provider}:{model_id}"
    key = api_key_for(provider)
    if not key and FEATURES[wire].keyed:
        raise ProviderUnavailable(f"{ref} (no {provider} API key on this host)", ref=ref)
    try:
        return CONSTRUCTORS[wire](model_id, base_url, key, throttle.client_for(provider))
    except (UserError, OpenAIError, AnthropicError) as refused:
        raise ProviderUnavailable(f"{ref} ({refused})", ref=ref) from refused
