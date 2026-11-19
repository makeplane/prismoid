# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one run loop. `base.build()` returns an `AgentRunner`, and every model run starts at `runner.run(prompt)`.

A run's failures leave in our own vocabulary (`model_incapable`). pydantic-ai raises `UnexpectedModelBehavior` when
a model exhausts its retries on the declared output shape, which is a different animal from a provider outage:
`FallbackModel` falls through on a rate limit or a 5xx, but a clean HTTP 200 full of unusable content satisfies the
chain, so a weak model at the head of a judge chain once silently killed verification for everything behind it. A
step whose model cannot comply has failed (retry or escalate); a judge whose model cannot comply is replaced by
another family. Both read the same `ModelIncapable`.

A step attempt runs on its call log (`log`, ADR-089) as a chain of segments: the prompt, then each
`Continue` - one more user line inside the trailing request of the conversation so far (a stopped
attempt's next) - and each `Resume`, a park's answer to the ending its replayed run stopped on. Every run replays the chain from its first segment: what the attempt already
logged is replayed, a replay that diverges fences once to the next generation, and the agent's
`PrismoidDurability` streams the live tap (`TapSlot`) from inside each model call. A guarded run that
dies says how its step's next attempt takes it up (`pickup`): it re-enters a run cut inside a call,
or continues one stopped at a logged point; a refused request is never re-sent. A park saved before
the call log seeds a fresh generation with its conversation (`FromHistory`).

A replay that diverges fences once: a park's answers are lost with the generation, and the run starts
over. A run's chain ends on the attempt's own latest segment for a prompt, and for a `Continue` or a
`Resume` on the segment logged under its key, else one appended; a `Resume` answers the ending the
replayed run stopped on (its deferred calls answered, or one more line for a run that ended without
any). A segment before the last that died hands the next its conversation as far as it got (its death
replays at the same point); its replayed events stay off the tap. A text deliverable the output limit
cuts is written on from the cut, never asked again (`CONTINUATIONS`).

The run's span is pydantic-ai's own agent-run span; the run's ids ride it as run metadata, never content.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import httpx
import httpx2
import logfire
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import (
    Agent,
    AgentRunResult,
    DeferredToolResults,
    RunContext,
    capture_run_messages,
)
from pydantic_ai.exceptions import (
    ContentFilterError,
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
    RunCancelled,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UserError,
)
from pydantic_ai.messages import (
    AgentStreamEvent,
    CachePoint,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    UserContent,
    UserPromptPart,
)

from prismoid.ai.agents import cancellation
from prismoid.ai.agents.cancellation import Cancelled
from prismoid.ai.agents.messages import continue_with
from prismoid.ai.agents.tap import MessageTap
from prismoid.ai.capabilities import durability
from prismoid.ai.capabilities.durability import CallLogScope, ReplayDiverged, RunSegment
from prismoid.ai.capabilities.loops import RECALL_FROM
from prismoid.ai.capabilities.output_checks import Checked
from prismoid.ai.deps import TaskDeps
from prismoid.ai.rules.budget import Envelope
from prismoid.errors import ModelIncapable
from prismoid.schemas.failures import FailureKind

if TYPE_CHECKING:
    from prismoid.ai.agents.base import AgentDef

# What a person sees at the end of a text answer the model's output limit cut off.
CUT_OFF_LINE: Final = "This was cut off at the model's output limit, so it is incomplete."
# What a deliverable the output limit cut is told: its next request writes on from the cut.
CONTINUE_LINE: Final = (
    "Your answer stopped at the output limit. Continue it exactly where it stopped: the next words only, "
    "nothing repeated and nothing before them."
)
CONTINUATIONS: Final = 2  # the requests a cut text deliverable may take to finish before it dies
_ANSWERED: Final = ("gate:", "in-place:")  # the keys of a segment that is a park's answer (`Resume`)

# The ways an agent run dies that `model_incapable` translates (httpx2: the gateway's clients).
_RUN_FAILURES: Final = (
    UnexpectedModelBehavior,
    ModelAPIError,
    FallbackExceptionGroup,
    httpx.HTTPError,
    httpx2.HTTPError,
    UsageLimitExceeded,
    ConnectionError,
)
_KEY_REFUSED: Final = frozenset({401, 403})  # a key the provider does not know, or will not let use the model
_REQUEST_REFUSED: Final = frozenset({400, 413})


@dataclass(slots=True)
class TapSlot:
    """The durability capability's event handler: it streams to the live tap of the run in flight."""

    tap: MessageTap | None = None

    async def __call__(self, ctx: RunContext[TaskDeps], stream: AsyncIterable[AgentStreamEvent]) -> None:
        if self.tap is not None:
            await self.tap(ctx, stream)
            return
        async for _ in stream:
            pass

    async def restart(self) -> None:
        """A redirected final answer (`Steering.on_redirect`): the tap in flight starts a new message."""
        if self.tap is not None:
            await self.tap.restart()


@dataclass(frozen=True, slots=True)
class Continue:
    """One more user line on the run so far, as its own segment: a stopped attempt's next (`attempt:<n>`)."""

    key: str
    line: str


@dataclass(frozen=True, slots=True)
class Resume:
    """A park's answer, as its own segment (`gate:<id>`, or `in-place:<n>` when no gate opened); what
    `answer` returns for the replayed run's ending is the segment's logged input."""

    key: str
    answer: Callable[[object], DeferredToolResults | str]


@dataclass(frozen=True, slots=True)
class FromHistory:
    """A park saved before the call log: its conversation seeds a fresh generation, continued by its
    answers or, with none, by one line."""

    history: list[ModelMessage]
    answers: DeferredToolResults | None
    line: str


@dataclass(frozen=True, slots=True)
class Cached:
    """A step attempt's prompt as a head several steps send byte for byte, behind a cache point, then its own
    tail: the head's reads hit the prompt cache from the second step on."""

    head: str
    tail: str

    def opening(self) -> list[ModelMessage]:
        """The head as the conversation's first request, its cache point after it."""
        return [ModelRequest(parts=[UserPromptPart([self.head, CachePoint()])])]


type Start = str | Sequence[UserContent] | Continue | Resume | FromHistory | Cached

_PROMPT: Final = "prompt"  # the key of an attempt's first segment
_SEED: Final = "v1"  # the key of a generation a v1 park's conversation seeds
_RESULTS: Final = TypeAdapter(DeferredToolResults)


@dataclass(frozen=True, slots=True)
class AgentRunner:
    """A built agent with the deps and definition it was built from. Made by `base.build()` only."""

    agent: Agent[TaskDeps, Any]
    deps: TaskDeps
    definition: AgentDef
    # The handler the agent's durability streams through (`build()` sets it when the run has a tap).
    live: TapSlot | None = field(default=None, kw_only=True)
    # What the output checks left on the last run's ending (`build()` shares it with its `OutputChecks`).
    checked: Checked = field(default_factory=Checked, kw_only=True)

    @property
    def envelope(self) -> Envelope:
        """The step's budget envelope: its request ceiling per segment and its wall."""
        return Envelope.for_step(guarded=self.definition.guarded)

    async def run(
        self, start: Start, *, log: CallLogScope | None = None, cancelled: Cancelled | None = None
    ) -> AgentRunResult[Any]:
        """One run; with `log`, a step attempt's segments on its call log; with `cancelled`, a cancel
        of its task cuts the run within about a second (`cancellation`)."""
        async with cancellation.watched(cancelled):
            return await self._fenced(start, log)

    async def _fenced(self, start: Start, log: CallLogScope | None) -> AgentRunResult[Any]:
        """The run, logged or not; a logged replay that diverges fences once and runs again."""
        if log is None:
            return await self._live(None, lambda tap: self._unlogged(start, tap))
        try:
            with durability.active(log):
                return await self._logged(start, log)
        except ReplayDiverged as diverged:
            logfire.warn("the call log's replay diverged, fencing: {why}", why=str(diverged))
            root = next((s for s in log.segments() if s.after is None), None)
            await log.fence()
        with durability.active(log):
            return await self._logged(start if root is None else _restarted(root), log)

    async def _logged(self, start: Start, log: CallLogScope) -> AgentRunResult[Any]:
        """A run on the open scope: the chain of segments `start` ends, and the answer it still owes."""
        log.rewind()
        chain, pending = await _chain(start, log)
        return await self._live(log, lambda tap: self._chained(chain, pending, log, tap))

    async def _live(
        self, log: CallLogScope | None, body: Callable[[MessageTap | None], Awaitable[AgentRunResult[Any]]]
    ) -> AgentRunResult[Any]:
        """The run on its live tap, its failures (a cancel's cut too) as `ModelIncapable`; the tap
        closes in `finally`, so an interrupted stream still ends with its `message.end`."""
        tap = self._tap()
        if self.live is not None:
            self.live.tap = tap
        try:
            return await body(tap)
        except RunCancelled as exc:
            # Its task was cancelled: the cut request left no result row, and nothing replays it.
            raise ModelIncapable("the task was cancelled while this step ran - stopped", infra=True) from exc
        except _RUN_FAILURES as exc:
            dead = model_incapable(exc, guarded=self.definition.guarded)
            if log is not None and self.definition.guarded and not dead.context.get("refused"):
                dead.pickup = "reenter" if log.cut else "continue"
            raise dead from exc
        finally:
            if self.live is not None:
                self.live.tap = None
            if tap is not None:
                await tap.close()

    def _tap(self) -> MessageTap | None:
        """The message lane, only where the caller wired `emit_event` (judges and the light agents never do)."""
        if self.deps.emit_event is None:
            return None
        pour_output, text_as_draft, quiet_stream = _stream_flags(self.definition)
        return MessageTap(
            self.deps,
            str(self.agent.name or ""),
            pour_output=pour_output,
            text_as_draft=text_as_draft,
            quiet_stream=quiet_stream,
        )

    async def _chained(
        self, chain: list[RunSegment], pending: Resume | None, log: CallLogScope, tap: MessageTap | None
    ) -> AgentRunResult[Any]:
        """Each segment of the chain in turn, the last one's result returned; a `pending` answer replays
        the whole chain first and logs its results as the next segment."""
        messages: list[ModelMessage] | None = None
        ended: AgentRunResult[Any] | None = None
        since = 0
        for segment in chain if pending is not None else chain[:-1]:
            log.segment = segment.n
            since = _recall_from(segment, messages, since)
            with capture_run_messages() as captured:
                try:
                    ended = await self._segment(segment, messages, tap, since=since, replayed=True)
                    messages = ended.all_messages()
                except _RUN_FAILURES:
                    ended, messages = None, list(captured)
        target = chain[-1] if pending is None else await _answered(pending, ended, chain[-1], log)
        log.segment = target.n
        since = _recall_from(target, messages, since)
        result = await self._segment(target, messages, tap, since=since)
        await log.returned(target)
        return result

    async def _segment(
        self,
        segment: RunSegment,
        history: list[ModelMessage] | None,
        tap: MessageTap | None,
        *,
        since: int = 0,
        replayed: bool = False,
    ) -> AgentRunResult[Any]:
        """One segment's `agent.run`: the prompt, the deferred calls of `history` answered, or its line
        inside the trailing request of `history`; `since` is where the run guard's evidence starts."""
        seeded = segment.history is not None
        if seeded:
            history = ModelMessagesTypeAdapter.validate_python(segment.history)
        try:
            if history is None:
                result = await self._agent_run(segment.line, tap=tap)
            elif segment.answers is not None:
                answers = _RESULTS.validate_python(segment.answers)
                result = await self._agent_run(history=history, answers=answers, tap=tap, since=since)
            else:
                result = await self._agent_run(
                    history=continue_with(history, segment.line), tap=tap, since=since
                )
        except UserError as exc:
            if not seeded:
                raise
            # Fail-soft: the ladder's next attempt starts fresh; logged, since a config fault raises this too.
            logfire.exception("a seeded run raised UserError: {agent}", agent=str(self.agent.name or ""))
            raise ModelIncapable(
                f"the saved conversation could not continue ({exc}) - starting over", refused=True
            ) from exc
        result = await self._finished(result, tap, since=since)
        return await _ended_honestly(
            result, None if replayed else tap, publishes=self.definition.publishes_output
        )

    async def _finished(
        self, result: AgentRunResult[Any], tap: MessageTap | None, *, since: int = 0
    ) -> AgentRunResult[Any]:
        """A text deliverable the output limit cut, written on from the cut by up to `CONTINUATIONS` more
        requests, each its own and the parts joined as one answer; anything else as it ended."""
        if not self.definition.publishes_output:
            return result
        parts: list[str] = []
        for _ in range(CONTINUATIONS):
            if not (isinstance(result.output, str) and result.response.finish_reason == "length"):
                break
            parts.append(result.output)
            history = continue_with(result.all_messages(), CONTINUE_LINE)
            result = await self._agent_run(history=history, tap=tap, since=since)
        if parts and isinstance(result.output, str):
            result.output = "".join([*parts, result.output])
        return result

    async def _unlogged(self, start: Start, tap: MessageTap | None) -> AgentRunResult[Any]:
        """A fresh run: only a step attempt's call log continues or resumes one."""
        if isinstance(start, Continue | Resume | FromHistory):
            raise TypeError("a continued or resumed run needs its call log")
        prompt = [start.head, CachePoint(), start.tail] if isinstance(start, Cached) else start
        result = await self._finished(await self._agent_run(prompt, tap=tap), tap)
        return await _ended_honestly(result, tap, publishes=self.definition.publishes_output)

    async def _agent_run(
        self,
        prompt: str | Sequence[UserContent] | None = None,
        *,
        history: list[ModelMessage] | None = None,
        answers: DeferredToolResults | None = None,
        tap: MessageTap | None,
        since: int = 0,
    ) -> AgentRunResult[Any]:
        """The one `agent.run` call, inside its envelope's limits, fresh per segment; `since` rides its
        metadata for the run guard (`RECALL_FROM`)."""
        return await self.agent.run(
            prompt,
            deps=self.deps,
            message_history=history,
            deferred_tool_results=answers,
            event_stream_handler=tap if self.live is None else None,  # a built agent's durability streams
            usage_limits=self.envelope.limits(),
            metadata={**_span_ids(self.deps), RECALL_FROM: since} if since else _span_ids(self.deps),
            cancellation_token=cancellation.token(),
        )


async def _chain(start: Start, log: CallLogScope) -> tuple[list[RunSegment], Resume | None]:
    """The segments a run of `start` replays and ends on, and a `Resume` still to answer after them."""
    segments = log.segments()
    by_n = {s.n: s for s in segments}
    pending: Resume | None = None
    if isinstance(start, Resume):
        target = next((s for s in segments if s.key == start.key), None)
        if target is None:
            target, pending = by_n[_head(segments)], start
    elif isinstance(start, Continue):
        logged = next((s for s in segments if s.key == start.key), None)
        target = logged or await log.append(start.key, start.line, after=_head(segments))
    elif isinstance(start, FromHistory):
        target = next((s for s in segments if s.key == _SEED), None) or await _seeded(start, log)
    else:
        target = segments[-1] if segments else None
        if target is None:
            target = await _prompted(start, log)
    by_n[target.n] = target
    chain = [target]
    while chain[-1].after is not None:
        chain.append(by_n[chain[-1].after])
    return chain[::-1], pending


async def _prompted(start: Start, log: CallLogScope) -> RunSegment:
    """A step attempt's prompt as its first segment; a `Cached` head is that segment's opening request."""
    if isinstance(start, Cached):
        history = ModelMessagesTypeAdapter.dump_python(start.opening(), mode="json")
        return await log.append(_PROMPT, _noted(start.tail, log.fence_note), after=None, history=history)
    if not isinstance(start, str):
        raise TypeError("a step attempt's prompt is text")
    return await log.append(_PROMPT, _noted(start, log.fence_note), after=None)


async def _seeded(start: FromHistory, log: CallLogScope) -> RunSegment:
    """A v1 park's conversation and its answers (or line), as the generation's first segment."""
    answers = None if start.answers is None else _RESULTS.dump_python(start.answers, mode="json")
    history = ModelMessagesTypeAdapter.dump_python(start.history, mode="json")
    return await log.append(_SEED, start.line, after=None, answers=answers, history=history)


async def _answered(
    pending: Resume, ended: AgentRunResult[Any] | None, head: RunSegment, log: CallLogScope
) -> RunSegment:
    """The park's answer to the ending its replayed run stopped on, logged as the next segment."""
    if ended is None:
        raise TypeError("a resume answers a run that ended")
    answer = pending.answer(ended.output)
    if isinstance(answer, str):
        return await log.append(pending.key, answer, after=head.n)
    answers = cast(JsonValue, _RESULTS.dump_python(answer, mode="json"))
    return await log.append(pending.key, "", after=head.n, answers=answers)


def _recall_from(segment: RunSegment, history: Sequence[ModelMessage] | None, since: int) -> int:
    """The responses before the latest park's answer: a person answering starts the guard's evidence afresh."""
    if history is None or not segment.key.startswith(_ANSWERED):
        return since
    return sum(isinstance(message, ModelResponse) for message in history)


def _restarted(root: RunSegment) -> Start:
    """A fenced run's first segment, run again on the next generation."""
    if root.history is None:
        return root.line
    answers = None if root.answers is None else _RESULTS.validate_python(root.answers)
    return FromHistory(ModelMessagesTypeAdapter.validate_python(root.history), answers, root.line)


def _head(segments: Sequence[RunSegment]) -> int:
    """The segment a new one continues: the latest, whether its run returned or died (its next
    attempt continues it)."""
    if not segments:
        raise TypeError("a continued run needs a logged prompt to continue")
    return segments[-1].n


def _noted(prompt: str, note: str | None) -> str:
    """The prompt, with a fenced attempt's note after it."""
    return prompt if note is None else f"{prompt}\n\n{note}"


def _span_ids(deps: TaskDeps) -> dict[str, str]:
    """The ids the run's span carries: task, step and conversation, whichever the run has."""
    ids = {"task_id": deps.task_id, "step_id": deps.step_id, "conversation_id": deps.conversation_id}
    return {key: str(value) for key, value in ids.items() if value is not None}


async def _ended_honestly(
    result: AgentRunResult[Any], tap: MessageTap | None, *, publishes: bool
) -> AgentRunResult[Any]:
    """A reply the token cap cut off says so, in its output and on its stream; a deliverable still cut
    after its continuations dies like a cut-off structured output (`UnexpectedModelBehavior`), so nothing
    half is published."""
    if isinstance(result.output, str) and result.response.finish_reason == "length":
        if publishes:
            raise UnexpectedModelBehavior("the deliverable was cut off at the model's output limit")
        result.output = f"{result.output}\n\n{CUT_OFF_LINE}"
        if tap is not None:
            await tap.append_text(CUT_OFF_LINE)
    return result


def _stream_flags(definition: AgentDef) -> tuple[bool, bool, bool]:
    """How the run streams: (pour_output, text_as_draft, quiet_stream). A publishing agent's output
    streams as the draft (typed: the output tool's args; text: its prose), unless the seat is quiet."""
    pour_output = text_as_draft = False
    if definition.publishes_output:
        pour_output = definition.output_ref is not None and not definition.quiet_stream
        text_as_draft = definition.output_ref is None
    return pour_output, text_as_draft, definition.quiet_stream


def model_incapable(exc: Exception, *, guarded: bool) -> ModelIncapable:
    """One of `_RUN_FAILURES`, as the attempt-death the ladder reads (`raise ... from exc`), its kind named
    here from the exception's class and status; the kind's flags steer the ladder (`schemas.failures`)."""
    if isinstance(exc, ContentFilterError):
        # A refusal like a 400/413: the same conversation would meet the same filter.
        said, kind = f"the provider's content filter refused the request: {exc}", FailureKind.REQUEST_REFUSED
    elif isinstance(exc, UnexpectedModelBehavior):
        said, kind = str(exc), FailureKind.MODEL_FAILED
    elif isinstance(exc, ModelAPIError):
        # A dropped stream or reset kills THIS attempt only; a 400/413 is the provider refusing the request.
        said = f"the provider connection failed mid-run: {exc}"
        refused = isinstance(exc, ModelHTTPError) and exc.status_code in _REQUEST_REFUSED
        kind = FailureKind.REQUEST_REFUSED if refused else _provider_kind([exc])
    elif isinstance(exc, FallbackExceptionGroup):
        # Every candidate failed (keys dead, credits out, runtime down): one attempt with a readable reason.
        reasons = "; ".join(str(e) for e in exc.exceptions[:3])
        said, kind = f"no model could take the call - {reasons}", _provider_kind(exc.exceptions)
    elif isinstance(exc, httpx.HTTPError | httpx2.HTTPError):
        # A 429 raised inside the response stream arrives as raw httpx, not ModelAPIError.
        said, kind = f"the provider connection failed mid-run: {exc}", FailureKind.PROVIDER_DOWN
    elif isinstance(exc, ConnectionError):
        # A tool's transport stopped answering (the sandbox, the device helper, a connector's server).
        said = f"the sandbox or a service the run relies on stopped answering: {exc}"
        kind = FailureKind.SERVICE_DOWN
    elif guarded:
        said = f"the run hit its request ceiling ({exc}); continue from where you stopped"
        kind = FailureKind.MODEL_FAILED
    else:
        # A spent request budget is an attempt-death, never a task-death; the wording teaches the retry.
        said = (
            f"the run exceeded its request budget ({exc}) - do the work in FEWER, larger "
            "steps: batch your file reads, build once, verify ONCE, publish"
        )
        kind = FailureKind.MODEL_FAILED
    return ModelIncapable(said, kind=kind)


def _provider_kind(failures: Sequence[BaseException]) -> FailureKind:
    """A refused key when every HTTP status among the failures is 401/403 (and one is), else the provider
    down: a refusal beside an outage on another family is not a key problem alone."""
    statuses = {e.status_code for e in failures if isinstance(e, ModelHTTPError)}
    return FailureKind.KEY_REFUSED if statuses and statuses <= _KEY_REFUSED else FailureKind.PROVIDER_DOWN
