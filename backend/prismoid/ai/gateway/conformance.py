# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Model conformance - may a catalog row serve a class whose runs stream text and end on a tool?

Adding a model is a row, and a row is only a claim. The judge streams its reasoning to the person
and ends the same response on its output tool; a model that answers in text alone, calls the tool
without a word, or cannot stream breaks that run. One check proves the shape against the one
specific model: stream a sentence, then call `final_answer`, in a single response.

`check_model` runs the check on any built `Model` (a `FunctionModel` offline); `run_conformance` is
`prismoid models evaluate`: it builds the row the way `resolve` does, metered, so the drill also proves
the price wiring (a call with no `UsageEvent` is a ledger hole). Persistence-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import EmitUsage, MeterContext, MeteredModel
from prismoid.ai.gateway.providers import build_model, features
from prismoid.schemas.model_class import ModelClass

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from prismoid.schemas.usage import UsageRecord

# The classes whose runs stream text and end on an output tool.
CONFORMANCE_CLASSES: frozenset[ModelClass] = frozenset({ModelClass.JUDGE})

OUTPUT_TOOL = "final_answer"
INSTRUCTIONS = (
    "Reply in one response with two parts: first one short plain-text sentence saying what you will "
    f"answer, then call the `{OUTPUT_TOOL}` tool with the answer."
)
PROMPT = "Which number is larger, 9 or 11?"
NOT_GATED = "no check - this class does not need streamed text ending on an output tool"


class _Answer(BaseModel):
    answer: str


@dataclass(frozen=True, slots=True)
class Conformance:
    """One model's result on the check; `detail` says what it did."""

    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ClassScore:
    model_class: ModelClass
    passed: int
    total: int
    note: str = ""  # why a class scored 0/0, or what the check saw

    @property
    def ratio(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    ref: str
    scores: tuple[ClassScore, ...]
    calls: int
    cost_usd: str
    metered: bool  # did every call emit a UsageEvent? a False here is a price-wiring hole

    @property
    def passed(self) -> int:
        return sum(s.passed for s in self.scores)

    @property
    def scored(self) -> int:
        return sum(s.total for s in self.scores)


def _shape(parts: list[object]) -> Conformance:
    """Whether one response's parts are streamed text followed by the output tool call."""
    from pydantic_ai.messages import TextPart, ToolCallPart

    text = next((i for i, p in enumerate(parts) if isinstance(p, TextPart) and p.content.strip()), None)
    tool = next(
        (i for i, p in enumerate(parts) if isinstance(p, ToolCallPart) and p.tool_name == OUTPUT_TOOL), None
    )
    if tool is None:
        return Conformance(False, "ended on text, not on the output tool")
    if text is None or text > tool:
        return Conformance(False, "called the output tool without streaming text first")
    return Conformance(True, "streams text and ends on the output tool in one response")


async def check_model(model: Model) -> Conformance:
    """Run the check once on `model`, streamed and capped at one response."""
    from pydantic_ai import Agent, AgentRunResultEvent, ToolOutput
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.usage import UsageLimits

    agent = Agent(model, instructions=INSTRUCTIONS, output_type=[ToolOutput(_Answer, name=OUTPUT_TOOL), str])
    try:
        async with agent.run_stream_events(PROMPT, usage_limits=UsageLimits(request_limit=1)) as events:
            results = [e.result async for e in events if isinstance(e, AgentRunResultEvent)]
    except Exception as exc:  # fail-closed: a model the check cannot run is a model that does not conform
        return Conformance(False, f"{type(exc).__name__}: {exc}"[:200])
    responses = [m for m in results[-1].all_messages() if isinstance(m, ModelResponse)]
    return _shape(list(responses[-1].parts))


async def run_conformance(
    spec: ModelSpec,
    classes: tuple[ModelClass, ...],
    *,
    emit_usage: EmitUsage,
    context: MeterContext | None = None,
) -> ConformanceReport:
    """Check `spec` once, metered, and score it for each of `classes` that needs conformance."""
    if not features(spec.protocol).chat:
        note = "media protocol - probe with `prismoid dev media-ping`"
        return ConformanceReport(
            spec.ref, tuple(ClassScore(c, 0, 0, note) for c in classes), 0, "0", metered=True
        )
    if not CONFORMANCE_CLASSES.intersection(classes):
        return ConformanceReport(
            spec.ref, tuple(ClassScore(c, 0, 0, NOT_GATED) for c in classes), 0, "0", metered=True
        )

    recorded: list[UsageRecord] = []

    async def _emit(record: UsageRecord) -> None:
        recorded.append(record)
        await emit_usage(record)

    built = build_model(
        provider=spec.provider, model_id=spec.model_id, protocol=spec.protocol, base_url=spec.base_url
    )
    result = await check_model(MeteredModel(built, spec, _emit, context or MeterContext()))
    scores = tuple(
        ClassScore(c, int(result.ok), 1, result.detail)
        if c in CONFORMANCE_CLASSES
        else ClassScore(c, 0, 0, NOT_GATED)
        for c in classes
    )
    cost = sum((getattr(r, "cost_usd", Decimal("0")) for r in recorded), start=Decimal("0"))
    return ConformanceReport(spec.ref, scores, calls=1, cost_usd=str(cost), metered=bool(recorded))
