# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run guard: brakes inside a guarded agent's run, at the steering safe
points - the same hooks `Steering` uses, applied beside it by `build()` when the definition is
`guarded`.

* before each model request - the task's spend is read (`TaskDeps.spend`, a closure operations
  wired; the engine stays persistence-free). At half and at three quarters of the cap one notice
  enters the conversation, once each; at the cap, or when the figures say the workspace is out of
  allowance (`exhausted`, whatever the cap), the guard ARMS a budget park. Queued stall words
  enter here too;
* before each tool dispatch - the spend is read again, and an armed guard defers the call (`CallDeferred`, marked
  BUDGET_METADATA): the run ends with the deferral, and operations parks the step on the BUDGET
  gate with its history saved. An agent that may not ask has no gate to park on: its every call
  but a way to finish is held instead, and the run ends with what it has (a further attempt is the
  envelope's to buy). A question or a connect block parks as itself. A model that ends
  the run instead ends it normally: output tools never pass these hooks. After a second stall
  every dispatch is held with the instruction to finish - but a question, a connect request or a
  hand-off, which end the run as a park, are a way to finish too;
* after each tool returns - the stall brake: the same call three times in the last eight, or the
  page five acting verbs brought back unchanged (or that the sidecar saw them change nothing), is a
  stall. "The same call" met the same thing: a
  browser act or look the same page, any other call the same files (an edit in between makes the
  next test run a new call: a `write_file` or `edit`, or a shell call after which the frame finds a
  file under `/work` newer than its mark, a logged check whose answer the call's return keeps for a
  replay). The first is told; the second says
  finish now. A run that still does not end meets the request ceiling (`run()`). The evidence is
  the step's, not the run's: a stopped run's next replays the calls its history already answered
  (`_recall`), but a person's answer to a park (a gate, a raised budget) starts the evidence afresh
  from the history's response the runner names (`RECALL_FROM`). A browser act is the element it
  named, read off the page the model held (`[e7.12]
  button "Next"` and `[e8.12] button "Next"` are one button), and a page is what it shows, its
  references' numbering aside - every look renumbers them. Two soft words come before any brake,
  once a run each: a page that came back unchanged `_SOFT_SAME_PAGES` times in a row
  (`PAGE_UNCHANGED`), and a lone field entry followed by another lone act on the same page - one
  call per response where a chain would do (`BATCH_REMINDER`).

Each run works on its own copy (`for_run`).

`budget_only`: an UNGUARDED step agent (a gather seat, a section writer) mounts the guard for its
budget alone - the spend read and, at the cap, every call but a way to finish held. No notice, no
stall brake, never a park.

A spend read that fails skips that safe point (logged): the envelope's between-attempt check is the
backstop for a blip. After `SPEND_READ_FAILURES_TO_ARM` failed reads in a row the guard fails
CLOSED: it arms the budget park as if the cap were met, because a run that cannot see its spend
must not keep spending. The next good read hands the decision back to the figures. The guard never
fails a run. Each spend read is a logged operation of the step's call log (`spend`, ADR-089), a
failed one included: a resumed attempt sees the figures the dead one saw at the same safe point."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import KW_ONLY, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, cast

import logfire
from pydantic import ValidationError
from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.exceptions import CallDeferred, SkipToolExecution
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition

from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.layout import TASK, WORK
from prismoid.ai.rules.budget import reached
from prismoid.ai.tools import catalog
from prismoid.ai.tools.ask import ASK_USER, REQUEST_CONNECTOR
from prismoid.ai.tools.browser import HANDOFF, TOOL_NAMES, PageState
from prismoid.ai.tools.call_rows import Said, never_ran, not_run, ran, say
from prismoid.ai.tools.frame_tools import PUBLISH
from prismoid.schemas.guard import (
    ALLOWANCE_FINISH,
    BATCH_REMINDER,
    BUDGET_FINISH,
    FINISH_NOW,
    FINISH_NOW_ANSWER,
    PAGE_UNCHANGED,
    STALL_HELD,
    budget_notice,
    stall_notice,
)
from prismoid.schemas.plan import BudgetFigures
from prismoid.schemas.steering import BUDGET_METADATA

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext

# The deferrals that park as themselves (the `Steering` law): a brake never takes their place.
_OWN_PARK = frozenset({ASK_USER, REQUEST_CONNECTOR})
# The calls that end a run are a way to finish, like the output tools: a run told to finish may
# still ask, request a connection, hand the browser to a person, or publish what it built (an
# agent that delivers through `publish` has no other way to hand its work over) - never held.
_FINISHES: Final = _OWN_PARK | {HANDOFF, PUBLISH}
# The browser verbs that act on a page (stall rule 2), the ones that start a new page (they reset
# its window), and the ones that only look (their key carries the page looked at, so looking again
# at a page that changed is not a repeat). Model-facing names.
_ACTS: Final = catalog.known(
    "browser_click", "browser_type", "browser_fill", "browser_press", "browser_select", "browser_scroll"
)
_NAVIGATES: Final = catalog.known("browser_open", "browser_back")
_LOOKS: Final = catalog.known("browser_snapshot", "browser_wait", "browser_screenshot")
# An act or a look is keyed on the page it met: the same verb on a page that moved is progress
# (reading a long page), and an act that leaves the page unchanged is rule 2's.
_ON_PAGE: Final = _LOOKS | _ACTS
# An edit starts a new window for every other call: the same command after it runs new code.
_EDITS: Final = catalog.known("write_file", "edit")
# A shell call is an edit when it changed a file under `/work`: the frame finds one newer than the mark.
_SHELL: Final = catalog.tool("shell").name
_MARK: Final = f"{TASK}/.guard-mark"
_CHANGED: Final = {"work": "changed"}  # a shell return's mark that the call changed `/work`
# The run metadata naming how many of the history's responses came before a person's answer.
RECALL_FROM: Final = "guard_recall_from"
_CALL_WINDOW: Final = 8
# Consecutive failed spend reads before the guard arms the budget park blind. One or two are a
# blip the envelope's own check covers; a third in a row is an outage the run must not spend through.
SPEND_READ_FAILURES_TO_ARM: Final = 3
_SAME_CALLS: Final = 3
_SAME_PAGES: Final = 5
# The soft word comes first: the same page this many times in a row, before rule 2's five.
_SOFT_SAME_PAGES: Final = 3
# The field entries a model could have chained with what came next (a submitting one could not).
_ENTRIES: Final = catalog.known("browser_type", "browser_fill", "browser_select")
# A search engine reads a query the same in any case or spacing, and a batch the same in any order,
# so the repeat rule does too. No other tool's arguments are touched: a command, a path or a typed
# text means what it says.
_SEARCH: Final = catalog.tool("web_search").name


def _said(messages: list[ModelMessage], lead: str) -> bool:
    """Has this conversation been told `lead`? A resumed run is a new guard: the history, not the
    guard's memory, is what was said."""
    return any(
        isinstance(p, UserPromptPart) and lead in str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
    )


def _state_of(tool: str, metadata: object) -> PageState | None:
    """The facts a browser answer carried, live or read back from the history (a dict there); None
    for an answer that carried none, and for any tool that is not the browser's."""
    if tool not in TOOL_NAMES or metadata is None:
        return None
    try:
        return PageState.model_validate(metadata)
    except ValidationError:  # a failed act's bare check carries no page: it neither counts nor breaks the run
        return None


def _as_elements(args: dict[str, Any], epoch: int | None) -> dict[str, Any]:
    """An act's arguments with each page reference read as its place on that page (`e8.12` is `#12`),
    so look-alike elements stay distinct whatever look numbered them."""
    held = f"e{epoch}." if epoch is not None else None

    def element(ref: object) -> object:
        if held is not None and isinstance(ref, str) and ref.startswith(held):
            return f"#{ref.removeprefix(held)}"
        return ref

    def field_of(one: object) -> object:  # a `fill` field, `{ref, value}`
        if not isinstance(one, dict):
            return one
        named = cast("dict[str, Any]", one)
        return {**named, "ref": element(named.get("ref"))}

    out = dict(args)
    if "ref" in out:
        out["ref"] = element(out["ref"])
    if isinstance(fields := out.get("fields"), list):
        out["fields"] = [field_of(one) for one in cast("list[object]", fields)]
    return out


def _lone(messages: list[ModelMessage]) -> bool:
    """Whether the response being run made exactly one call."""
    last = messages[-1] if messages else None
    return isinstance(last, ModelResponse) and sum(isinstance(p, ToolCallPart) for p in last.parts) == 1


def _key(tool: str, args: dict[str, Any], page: str, edits: int, epoch: int | None) -> str:
    """A call's identity for the repeat rule: name, arguments (acts name elements) and what it met, the
    page for a browser call or the edits so far otherwise; an edit is keyed on its content alone."""
    if tool == _SEARCH and isinstance(queries := args.get("queries"), list):
        asked = {" ".join(str(query).casefold().split()) for query in cast(list[object], queries)}
        args = {**args, "queries": sorted(asked)}
    if tool in _ACTS:
        args = _as_elements(args, epoch)
    canon = json.dumps(args, sort_keys=True, default=str)
    seen = page if tool in _ON_PAGE else "" if tool in _EDITS else str(edits)
    return hashlib.sha256(f"{tool}\x00{canon}\x00{seen}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class _SpendRead:
    """One logged spend read: the task's figures (`None`: it has no cap), or a read that failed."""

    figures: BudgetFigures | None = None
    failed: bool = False


@dataclass
class RunGuard(AbstractCapability[TaskDeps]):
    can_park: bool = False  # the agent's output type admits a deferral (an asking agent)
    outcome: bool = True  # the agent ends with `done(Outcome)`; else with its answer (no `done`)
    budget_only: bool = False  # an unguarded agent: the cap's hold alone (never parks, no words)
    budget_armed: bool = field(default=False, init=False)
    # What a call held at the cap is told: the budget's line, or the allowance's when it ran out.
    _hold: str = field(default=BUDGET_FINISH, init=False, repr=False)
    stalls: int = field(default=0, init=False)
    finish_now: bool = field(default=False, init=False)
    _queued: list[str] = field(default_factory=list[str], init=False, repr=False)
    _calls: deque[str] = field(
        default_factory=lambda: deque[str](maxlen=_CALL_WINDOW), init=False, repr=False
    )
    _pages: deque[str] = field(default_factory=lambda: deque[str](maxlen=_SAME_PAGES), init=False, repr=False)
    _page: str = field(default="", init=False, repr=False)  # the last page any browser return brought
    _epoch: int | None = field(default=None, init=False, repr=False)  # the look that numbered it
    _same: int = field(default=0, init=False, repr=False)  # returns in a row that brought `_page`
    _entry: str | None = field(default=None, init=False, repr=False)  # where the last lone entry was
    _soft: set[str] = field(default_factory=set[str], init=False, repr=False)  # the soft words said
    _edits: int = field(default=0, init=False, repr=False)  # the edits that have returned so far
    _recalled: bool = field(default=False, init=False, repr=False)  # the history's evidence is in
    _spend_failures: int = field(default=0, init=False, repr=False)  # failed spend reads in a row
    _: KW_ONLY
    id: str | None = "loops"  # pinned: the call log names its reads by it

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None  # run-scoped state; never spec-constructed

    async def _figures(self, ctx: RunContext[TaskDeps]) -> BudgetFigures | None:
        if ctx.deps.spend is None:
            return None
        read = await self._read_spend(ctx)
        if read.failed:
            self._spend_failures += 1
            if self._spend_failures >= SPEND_READ_FAILURES_TO_ARM:
                logfire.warn(
                    "the run guard could not read the task's spend {n} times in a row; it arms the budget park",
                    n=self._spend_failures,
                )
                self.budget_armed = True
            return None
        if self._spend_failures >= SPEND_READ_FAILURES_TO_ARM:
            self.budget_armed = False  # sight is back: the figures decide again
        self._spend_failures = 0
        return read.figures

    async def _rearm(self, ctx: RunContext[TaskDeps]) -> None:
        """Arm the cap's hold when a fresh spend read has reached it. Only arms: a failed read, or a
        raise, is the next request's to weigh (its read counts toward failing closed). Every dispatch
        reads, armed or not, so parallel siblings log as many reads live as they replay."""
        if ctx.deps.spend is None:
            return
        read = await self._read_spend_at_dispatch(ctx)
        if read.figures is not None and read.figures.ceiling_usd > 0 and reached(read.figures):
            self.budget_armed = True

    @durable_operation(name="spend")
    async def _read_spend(self, ctx: RunContext[TaskDeps]) -> _SpendRead:
        """The task's spend as of this request, logged; a read that failed is logged as one."""
        return await self._spend_now(ctx)

    @durable_operation(name="spend_at_dispatch")
    async def _read_spend_at_dispatch(self, ctx: RunContext[TaskDeps]) -> _SpendRead:
        """The spend as of this dispatch, logged under its own name: a log written before dispatch reads
        holds none, so its request reads replay as themselves and this one runs live."""
        return await self._spend_now(ctx)

    @staticmethod
    async def _spend_now(ctx: RunContext[TaskDeps]) -> _SpendRead:
        if ctx.deps.spend is None:
            return _SpendRead()
        try:
            return _SpendRead(await ctx.deps.spend())
        except Exception:
            logfire.exception("the run guard could not read the task's spend; this safe point is skipped")
            return _SpendRead(failed=True)

    @durable_operation(name="work")
    async def _work_changed(self, ctx: RunContext[TaskDeps]) -> bool:
        """Whether a file under `/work` changed since the last check (the first: true), logged; the mark moves."""
        frame = ctx.deps.frame
        if frame is None:
            return False
        try:
            found = await frame.exec(
                f"test -e {_MARK} && find {WORK} -newer {_MARK} -type f 2>/dev/null | head -n 1"
            )
            await frame.write_bytes(_MARK, b"", mode=0o444, owner="system")
        except Exception:
            # Fail-soft: a check that could not run counts as progress, so it never stops a working step.
            logfire.exception(
                "the run guard could not check the work folder; the shell call counts as an edit"
            )
            return True
        return not found.ok or bool(found.stdout.strip())

    @property
    def _finish_now(self) -> str:
        return FINISH_NOW if self.outcome else FINISH_NOW_ANSWER

    async def for_run(self, ctx: RunContext[TaskDeps]) -> RunGuard:
        """A fresh copy per run (the state fields are `init=False`): a run told to finish never holds
        the next run's calls on the same agent."""
        return replace(self)

    def _recall(self, messages: list[ModelMessage], since: int = 0) -> None:
        """Before a continued run's first safe point, replay the calls its history already answered through
        the same rules (skipping calls that never ran); the evidence starts afresh after response `since`."""
        if self._recalled:
            return
        self._recalled = True
        responses = 0
        answered = {
            part.tool_call_id: part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if ran(part) and not never_ran(part)
        }
        made: dict[str, tuple[dict[str, Any], bool]] = {}  # each call's arguments, and was it alone
        for message in messages:
            if isinstance(message, ModelResponse):
                self._answered_at(responses, since)
                responses += 1
            for part in message.parts:
                if isinstance(part, ToolCallPart) and part.tool_call_id in answered:
                    made[part.tool_call_id] = (part.args_as_dict(), _lone([message]))
                    self._called(part.tool_name, made[part.tool_call_id][0])
                elif ran(part) and part.tool_call_id in answered:
                    args, lone = made.get(part.tool_call_id, ({}, False))
                    state = _state_of(part.tool_name, part.metadata)
                    edited = part.tool_name == _SHELL and part.metadata == _CHANGED
                    self._saw(
                        part.tool_name, state, lone=lone, submits=bool(args.get("submit")), edited=edited
                    )
        self._answered_at(responses, since)
        self._queued.clear()
        if self.stalls:
            logfire.info("run guard: the history held {n} stall(s)", n=self.stalls)

    def _answered_at(self, responses: int, since: int) -> None:
        """A person answered after response `since`: the brake's evidence and its stalls start again."""
        if since and responses == since:
            self.stalls, self.finish_now = 0, False
            self._calls.clear()
            self._pages.clear()

    def _recall_safely(self, ctx: RunContext[TaskDeps], messages: list[ModelMessage]) -> None:
        try:
            self._recall(messages, int((ctx.metadata or {}).get(RECALL_FROM, 0)))
        except Exception:
            logfire.exception("the run guard could not read the history; it starts from this run")

    def _called(self, tool: str, args: dict[str, Any]) -> None:
        key = _key(tool, args, self._page, self._edits, self._epoch)
        self._calls.append(key)
        if self._calls.count(key) >= _SAME_CALLS:
            self._stalled(tool)

    def _stalled(self, tool: str) -> None:
        self.stalls += 1
        self._calls.clear()  # the next stall needs fresh evidence
        self._pages.clear()
        logfire.info("run guard: stall {n} on {tool}", n=self.stalls, tool=tool)
        if self.stalls == 1:
            self._queued.append(stall_notice(tool, outcome=self.outcome))
        else:
            self.finish_now = True
            self._queued.append(self._finish_now)

    async def _journal_held(self, ctx: RunContext[TaskDeps], call: ToolCallPart) -> None:
        held = Said(
            catalog.journal_name(call.tool_name),
            False,
            denied_reason=STALL_HELD,
            tool_call_id=call.tool_call_id,
        )
        try:
            await say(ctx.deps.journal_tool, held)
        except Exception:
            logfire.exception("the run guard could not journal a held call; the run goes on")

    def _arm(self, figures: BudgetFigures) -> None:
        """The figures decide: armed at the cap or with the workspace's allowance spent, each held call
        told which of the two it was."""
        self.budget_armed = reached(figures)
        self._hold = ALLOWANCE_FINISH if figures.exhausted else BUDGET_FINISH

    async def before_model_request(
        self, ctx: RunContext[TaskDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if self.budget_only:
            figures = await self._figures(ctx)
            if figures is not None:
                self._arm(figures)
            return request_context
        self._recall_safely(ctx, list(request_context.messages))
        lines, self._queued = self._queued, []
        figures = await self._figures(ctx)
        if figures is not None:
            self._arm(figures)  # a raised cap, or the allowance back, disarms
            notice = None if self.budget_armed else budget_notice(figures, can_park=self.can_park)
            if notice is not None and not _said(list(request_context.messages), notice.lead):
                lines.append(notice.text)
        if lines:
            ctx.enqueue(UserPromptPart("\n\n".join(lines)), priority="asap")  # this request and the history
        return request_context

    async def before_tool_execute(
        self,
        ctx: RunContext[TaskDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        # The spend as of THIS dispatch, not the request's: a sibling seat, an earlier call of this
        # batch or a priced tool may have reached the cap since (a wave of calls spends before
        # the next request reads it).
        await self._rearm(ctx)
        if self.budget_only:
            if self.budget_armed and call.tool_name not in _FINISHES:
                await self._journal_held(ctx, call)
                raise SkipToolExecution(not_run(self._hold))
            return args
        self._recall_safely(ctx, ctx.messages)  # a resume's leftover calls run before its first request
        if self.budget_armed and self.can_park and call.tool_name not in _OWN_PARK:
            raise CallDeferred(metadata=dict(BUDGET_METADATA))
        if self.budget_armed and not self.can_park and call.tool_name not in _FINISHES:
            # No gate to pause on: hold it as a stall's finish does, and the run ends with what it has.
            await self._journal_held(ctx, call)
            raise SkipToolExecution(not_run(self._hold))
        if self.finish_now and call.tool_name not in _FINISHES:
            await self._journal_held(ctx, call)
            raise SkipToolExecution(not_run(self._finish_now))
        try:
            # The model's own arguments, as the history keeps them: a replayed call keys the same.
            self._called(call.tool_name, call.args_as_dict())
        except Exception:
            logfire.exception("the run guard could not read a call; the run goes on")
        return args

    async def after_tool_execute(
        self,
        ctx: RunContext[TaskDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        if self.budget_only:
            return result
        edited = call.tool_name == _SHELL and isinstance(result, str) and await self._work_changed(ctx)
        if edited:
            result = ToolReturn(result, metadata=dict(_CHANGED))  # a replay's recall counts it again
        try:
            submits = bool(call.args_as_dict().get("submit"))
            returned: object = result  # the tool's own return: its text, or a `ToolReturn` with facts
            state = _state_of(call.tool_name, returned.metadata if isinstance(returned, ToolReturn) else None)
            self._saw(call.tool_name, state, lone=_lone(ctx.messages), submits=submits, edited=edited)
        except Exception:
            logfire.exception("the run guard could not read a tool's return; the run goes on")
        return result

    def _saw(
        self,
        tool: str,
        state: PageState | None,
        *,
        lone: bool = False,
        submits: bool = False,
        edited: bool = False,
    ) -> None:
        if tool in _EDITS or edited:
            self._edits += 1
        if tool in _NAVIGATES:
            self._pages.clear()
        self._chained(tool, state, lone=lone, submits=submits)
        if state is None or (page := state.page) is None:
            return  # no page: an acting verb that brought none neither counts nor breaks the run
        self._same = self._same + 1 if page == self._page else 1
        self._page, self._epoch = page, state.epoch
        if self._same >= _SOFT_SAME_PAGES:
            self._say_once(PAGE_UNCHANGED)
        if tool in _ACTS:
            # An act the sidecar saw change nothing met the page the act before it met, whatever
            # the tree shows meanwhile (a clock, a rotating banner): rule 2 counts it as that page.
            unmoved = state.unmoved and self._pages
            self._pages.append(self._pages[-1] if unmoved else page)
            if len(self._pages) == _SAME_PAGES and len(set(self._pages)) == 1:
                self._stalled(tool)

    def _chained(self, tool: str, state: PageState | None, *, lone: bool, submits: bool) -> None:
        """A response that made one call, a field entry, then another that made one act on the same
        page: the two could have been one response (`BATCH_REMINDER`). Any other call breaks it."""
        where = state.where if lone and state is not None and tool in _ACTS else None
        if where is not None and where == self._entry:
            self._say_once(BATCH_REMINDER)
        self._entry = where if tool in _ENTRIES and not submits else None

    def _say_once(self, word: str) -> None:
        if word not in self._soft:
            self._soft.add(word)
            self._queued.append(word)
