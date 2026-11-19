# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ways a run stops without ending: each `Deferral` a run hands the ladder, and what its park
saves. A deferral is a value returned, never an exception, so the frame stays alive for the resume;
the turn it came from decides which one (`control_deferral`, `approval_deferral`, `budget_park_of`),
and `park_record` is its share of the row's `resume_state`. The run itself is on the step's call log, so
a park saves no conversation and no call ids: its `ParkRecord`, and the resume reads the deferred
calls again off the replayed run (`classify.deferral_of`).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NamedTuple

import logfire
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ToolCallPart

from prismoid.ai.rules.budget import reached
from prismoid.ai.tools.ask import ASK_USER, REQUEST_CONNECTOR
from prismoid.schemas.outputs import Outcome
from prismoid.schemas.park import ParkKind, ParkRecord
from prismoid.schemas.steering import BUDGET_METADATA

if TYPE_CHECKING:
    from prismoid.db.models.artifact import Artifact
    from prismoid.schemas.lead import Decline, Deliver, Fail, ProposeTask, Reply, SubmitPlan
    from prismoid.schemas.longform import OutlineResult
    from prismoid.schemas.plan import BudgetFigures, QuestionSet
    from prismoid.schemas.swarm import RecordSet, ScatterResult

# Every OTHER pending id of a turn that parked on a person at the browser (a second held call, an
# ask, a connect request, an approval): pydantic-ai refuses partial results, so each is answered.
NOT_RUN_FOR_CONTROL = (
    "not run: the browser was handed to a person first - take a new snapshot and call this again "
    "if it is still needed"
)


@dataclass(frozen=True)
class Deferral:
    """A step that stopped mid-run to ask the user one question batch; the frame stays alive for the resume.
    `denied_approvals`: approvals sharing the turn, denied with RETRY_AFTER_ANSWER (one gate at a time)."""

    tool_call_id: str
    questions: list[tuple[str, list[str]]]
    denied_approvals: tuple[str, ...] = ()
    # Other pending CALLS in the same turn (a request_connector beside the ask): answered with
    # a re-request-later string on resume - pydantic-ai refuses partial results.
    extra_calls: tuple[str, ...] = ()
    # SECOND ask_user calls in the same turn (models batch parallel calls despite the one-batch
    # instruction): answered with the one-batch string on resume - an unresolved id would make
    # pydantic-ai reject the whole DeferredToolResults and fail the task after the user answered.
    extra_asks: tuple[str, ...] = ()
    # A browser step's `once` widens at park time: its live allowlist (`deps.browser.allowed`)
    # less what the allowlist's homes vouched for when this run began. A resume recomputes the
    # homes, which never hold a `once`, and its `frame.allow` REPLACES the proxy list.
    # Persisted on the park's record (`ParkRecord.browser_once`).
    browser_once: tuple[str, ...] = ()
    # The newest steer epoch the run was told before it parked: the resume restores
    # it, so the resumed run is never told the same steer twice. Persisted as `ParkRecord.told_epoch`.
    told: int = 0
    # Sibling calls a pause deferred in the same turn: the question parks as
    # itself, and the resume tells each of these it did not run, `paused_by` naming who paused.
    # `paused_by` is persisted as `ParkRecord.paused_by`.
    paused_calls: tuple[str, ...] = ()
    paused_by: str = ""
    # A batch built whole, with its defaults, TTL and `on_timeout` (the Lead's ask, through the intake's
    # own builder): its gate opens as it is, and `questions` stays empty. None: `questions` is the batch.
    asked: QuestionSet | None = None


@dataclass(frozen=True)
class ConnectDeferral:
    """A step that stopped (`request_connector`) because it needs a service the task does not hold; parks
    on a CONNECT gate, and the resume answers the call with what happened."""

    tool_call_id: str
    service: str
    why: str
    extra_calls: tuple[str, ...] = ()
    denied_approvals: tuple[str, ...] = ()
    # ask_user calls that shared the turn but could not park (unreadable batch, or the connect
    # outranked them): answered with the one-batch string on resume, never left unresolved.
    extra_asks: tuple[str, ...] = ()
    browser_once: tuple[str, ...] = ()  # see `Deferral.browser_once`
    told: int = 0  # see `Deferral.told`
    paused_calls: tuple[str, ...] = ()  # see `Deferral.paused_calls`
    paused_by: str = ""


@dataclass(frozen=True)
class ApprovalDeferral:
    """A step that stopped for consent to a batch of side-effectful calls (`items`); one gate covers all.
    `source` is browser only when every call is; `host`/`scopes` only for a lone allowlist ask."""

    items: list[tuple[str, str, str, str]]
    source: str = "connector"
    host: str = ""
    scopes: list[str] = field(default_factory=list)
    fingerprints: dict[str, dict[str, str] | None] = field(default_factory=dict)
    browser_once: tuple[str, ...] = ()  # see `Deferral.browser_once`
    told: int = 0  # see `Deferral.told`
    whys: tuple[str, ...] = ()  # why the approval rules asked (`Rule.why`), each once, in call order


@dataclass(frozen=True)
class PauseDeferral:
    """A paused task's step parked at its next tool dispatch: a question-less gate that never times out;
    on resume every pending call is told it was paused before it ran."""

    calls: tuple[str, ...]
    told: int = 0  # see `Deferral.told`
    denied_approvals: tuple[str, ...] = ()
    paused_by: str = ""
    browser_once: tuple[str, ...] = ()  # see `Deferral.browser_once`
    extra_asks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlDeferral:
    """A step that stopped because a person is at the browser (a takeover lease or `browser.handoff`); one
    control call parks on a CONTROL gate, and the turn's other pending calls are told they did not run."""

    tool_call_id: str
    source: str  # "handoff" | "takeover"
    reason: str
    holder: str
    extra_calls: tuple[str, ...] = ()
    denied_approvals: tuple[str, ...] = ()
    extra_asks: tuple[str, ...] = ()
    browser_once: tuple[str, ...] = ()  # see `Deferral.browser_once`
    since: str = ""  # ISO: the lease's `since`, as the hook read it (takeover only)
    host: str = ""  # the page's host, as the sidecar read it at the handoff (handoff only)
    told: int = 0  # see `Deferral.told`
    paused_calls: tuple[str, ...] = ()  # see `Deferral.paused_calls`
    paused_by: str = ""
    # The Outcome a hand-off nudge answered: a sign-in block on a Machine
    # with a screen. The ladder puts it on the RUNNING row while the gate is open.
    ended: Outcome | None = None


@dataclass(frozen=True)
class BudgetDeferral:
    """A guarded step that reached its task's cap mid-run, parked on the BUDGET gate: accept resumes the
    same run, decline settles the step `partial` with no model call."""

    calls: tuple[str, ...]  # every pending call of the turn but a question: none of them ran
    approvals: tuple[str, ...] = ()  # approval asks that shared the turn: denied on resume
    # A question or a connect request that shared the turn: it parks as itself (the guard never
    # holds it), so it is told to ask again - the budget's gate took the turn, not the budget.
    asks: tuple[str, ...] = ()
    told: int = 0  # see `Deferral.told`
    browser_once: tuple[str, ...] = ()  # see `Deferral.browser_once`
    # The model's own stop at the cap (no call pends): the step's blocked ending when the Bursar
    # sees nothing over - there is then no gate to park on and no raise to tell it about.
    ended: Outcome | None = None


# Every park a run can hand the ladder: one `Deferral` per way a run stops without ending.
type ParkDeferral = (
    Deferral | ConnectDeferral | ApprovalDeferral | PauseDeferral | ControlDeferral | BudgetDeferral
)


class Produced(NamedTuple):
    """A run that ended: its narration, the artifacts it delivered, and its typed slot for the completion
    path (outline, scatter map, records, Lead ending or `Outcome`)."""

    narration: str
    artifacts: list[Artifact]
    outline: (
        OutlineResult
        | ScatterResult
        | RecordSet
        | Outcome
        | Reply
        | SubmitPlan
        | ProposeTask
        | Fail
        | Deliver
        | Decline
        | None
    )


# One attempt's result, tagged by its type: it produced, or it parked on one of the deferrals.
type AttemptResult = Produced | ParkDeferral


def park_record(deferral: ParkDeferral, kind: ParkKind, gate_id: uuid.UUID | None) -> dict[str, object]:
    """The park's record as its `resume_state` share; `gate_id` None when its call is answered in place."""
    paused = isinstance(deferral, Deferral | ConnectDeferral | ControlDeferral | PauseDeferral)
    record = ParkRecord(
        kind=kind,
        gate_id=gate_id,
        told_epoch=deferral.told,
        browser_once=deferral.browser_once,
        paused_by=deferral.paused_by if paused else "",
    )
    return record.saved()


def budget_state(deferral: BudgetDeferral, gate_id: uuid.UUID | None) -> dict[str, object]:
    """A budget park's `resume_state`: what the resume needs to finish the same run."""
    return {
        **park_record(deferral, ParkKind.BUDGET, gate_id),
        # A `partial` the run ended on at the cap: keeping the cap settles the step on the model's
        # own account of what it did, never the generic line.
        **(
            {"budget_ended": deferral.ended.model_dump(mode="json")}
            if deferral.ended is not None and deferral.ended.status == "partial"
            else {}
        ),
    }


def budget_deferral(result: Any, *, told: int) -> BudgetDeferral | None:
    """The run's budget park, if it ended on one: a turn the run guard
    deferred (any call marked BUDGET_METADATA - every pending id rides it, none ran), or the
    model's own `blocked_by="budget"` stop (no call). A person at the browser outranks it: a turn
    with a control call is the CONTROL gate's. None for anything else."""
    output = result.output
    if isinstance(output, DeferredToolRequests):
        held = any(output.metadata.get(c.tool_call_id) == BUDGET_METADATA for c in output.calls)
        if not held or control_calls(output):
            return None
        return budget_held(output, told=told)
    if isinstance(output, Outcome) and output.status == "blocked" and output.blocked_by == "budget":
        return BudgetDeferral(calls=(), told=told, ended=output)
    return None


def budget_held(output: DeferredToolRequests, *, told: int) -> BudgetDeferral:
    """A turn the run guard held at the cap, as its park: a question or a connect request parks as
    itself (`asks`), every other call never ran, and the approvals beside them are denied."""
    own = {c.tool_call_id for c in output.calls if c.tool_name in (ASK_USER, REQUEST_CONNECTOR)}
    return BudgetDeferral(
        calls=tuple(c.tool_call_id for c in output.calls if c.tool_call_id not in own),
        asks=tuple(c.tool_call_id for c in output.calls if c.tool_call_id in own),
        approvals=tuple(p.tool_call_id for p in output.approvals),
        told=told,
    )


async def budget_park_of(
    result: Any, *, told: int, spend: Callable[[], Awaitable[BudgetFigures | None]] | None
) -> BudgetDeferral | None:
    """The run's budget park: `budget_deferral`'s, or a `partial` it stopped short on at the cap."""
    budget = budget_deferral(result, told=told)
    if budget is None and await stopped_short_at_the_cap(result, spend):
        budget = BudgetDeferral(calls=(), told=told, ended=result.output)
    return budget


async def stopped_short_at_the_cap(
    result: Any, spend: Callable[[], Awaitable[BudgetFigures | None]] | None
) -> bool:
    """A `partial` ending at the task's cap, or with its workspace's allowance spent (ADR-103), is a
    budget stop too, so the person decides (an allowance: its return resumes it); under the cap (or when the
    spend read fails) it ends as itself."""
    if spend is None or not (isinstance(result.output, Outcome) and result.output.status == "partial"):
        return False
    try:
        figures = await spend()
    except Exception:
        logfire.exception("could not read the task's spend; a partial ending stands")
        return False
    return reached(figures)


async def browser_park[
    P: Deferral | ConnectDeferral | ApprovalDeferral | PauseDeferral | ControlDeferral | BudgetDeferral
](frame: Any, browser: Any, deferral: P, *, vouched: set[str]) -> P:
    """A browser step is about to park (an approval, a question, a connect, a pause): pin its
    Chromium alive (`hold` - the sidecar's idle stop is 10 minutes, a gate's TTL is a day; without
    it the resumed click met a stale reference and a second gate) and carry its `once` widens -
    the live allowlist less what the allowlist's homes `vouched` for when this run began - into the
    park, so the resume's `frame.allow` (which REPLACES the proxy list) never drops a host the
    human already let through. What the homes vouched for is theirs to say again on resume: a
    grant revoked meanwhile is not carried back by the park. Loopback never rides it: it is a
    toolset-local allowance (the computer's own server), never a proxy host. Best-effort: a
    failed `hold` costs a re-snapshot after a long park, never the park itself."""
    if browser is None:
        return deferral
    from prismoid.schemas.egress import is_localhost

    with contextlib.suppress(Exception):
        await frame.browser({"verb": "hold"})
    once = tuple(sorted(h for h in browser.allowed - vouched if not is_localhost(h)))
    return replace(deferral, browser_once=once)


def approval_deferral(output: DeferredToolRequests) -> ApprovalDeferral:
    """Consent-only turn → `ApprovalDeferral`. Pure - no
    session, no agent run - so the source/host/scopes/fingerprints wiring is unit-testable
    against a hand-built `DeferredToolRequests`, not just proven live end to end.

    The gate's evidence comes from the hook's metadata (connector, tool, capped preview); a
    call whose metadata went missing still gets a keys-only line - the park must not depend
    on it. `source` is "browser" ONLY when EVERY approval in the batch is browser-sourced -
    accept approves the whole batch (one batch per gate), so a connector call riding
    alongside a browser one must make the WHOLE gate "connector" (creator-only), never a
    mixed gate an editor could wave a connector call through. `host`/`scopes` are set ONLY
    for a lone `browser_open` allowlist ask (the one shape with something to choose a scope
    over) - any other browser-only batch (a danger click, or an allowlist ask sharing the
    turn with something else) carries no scopes, and accepting it means "once": the
    toolset's own frame-widen, never a task-level grant this gate would otherwise
    mis-describe."""
    items: list[tuple[str, str, str, str]] = []
    fingerprints: dict[str, dict[str, str] | None] = {}
    metas: list[dict[str, Any]] = []
    for part in output.approvals:
        meta = output.metadata.get(part.tool_call_id) or {}
        metas.append(meta)
        items.append(
            (
                part.tool_call_id,
                str(meta.get("connector_name") or part.tool_name.split("_", 1)[0]),
                str(meta.get("tool") or part.tool_name),
                str(meta.get("preview") or "args: " + ", ".join(sorted(part.args_as_dict()))),
            )
        )
        fp = meta.get("fingerprint")
        fingerprints[part.tool_call_id] = fp if isinstance(fp, dict) else None

    all_browser = bool(metas) and all(str(m.get("source", "connector")) == "browser" for m in metas)
    source = "browser" if all_browser else "connector"
    host = ""
    scopes: list[str] = []
    if all_browser and len(metas) == 1:
        meta0 = metas[0]
        offered = [str(s) for s in (meta0.get("scopes") or [])]
        if str(meta0.get("tool", "")) == "browser.open" and offered:
            host = str(meta0.get("host", ""))
            scopes = offered

    whys = tuple(dict.fromkeys(str(m["why"]) for m in metas if m.get("why")))
    return ApprovalDeferral(
        items=items, source=source, host=host, scopes=scopes, fingerprints=fingerprints, whys=whys
    )


def control_calls(output: DeferredToolRequests) -> list[ToolCallPart]:
    """The deferred calls a person at the browser raised: a call that met a live
    lease (`CONTROL_HELD`, any verb's name) or the agent's `browser.handoff` (`HANDOFF`). Read
    off the metadata's `kind`, never the tool name."""
    from prismoid.ai.tools.browser import CONTROL_HELD, HANDOFF

    kinds = (CONTROL_HELD, HANDOFF)
    return [
        c for c in output.calls if str((output.metadata.get(c.tool_call_id) or {}).get("kind", "")) in kinds
    ]


def control_deferral(output: DeferredToolRequests) -> ControlDeferral:
    """A turn with a control call → `ControlDeferral`. Pure, like
    `approval_deferral`. A person at the browser outranks every other ask: the FIRST control
    call parks; a second held call and any connect request ride `extra_calls`, asks
    `extra_asks`, approvals `denied_approvals` - all answered `NOT_RUN_FOR_CONTROL` on resume,
    because pydantic-ai refuses partial results. A call that met a live lease outranks the agent's
    own handoff in the same turn: someone is already driving, so the step parks as a takeover
    (unbudgeted) and the handoff rides `extra_calls`. ValueError when the turn holds no control
    call."""
    from prismoid.ai.tools.ask import ASK_USER
    from prismoid.ai.tools.browser import CONTROL_HELD, HANDOFF

    controls = control_calls(output)
    if not controls:
        raise ValueError("no control call in this turn")

    def kind(c: ToolCallPart) -> str:
        return str((output.metadata.get(c.tool_call_id) or {}).get("kind", ""))

    first = next((c for c in controls if kind(c) == CONTROL_HELD), controls[0])
    meta = output.metadata.get(first.tool_call_id) or {}
    held = {c.tool_call_id for c in controls}
    others = [c for c in output.calls if c.tool_call_id not in held]
    return ControlDeferral(
        tool_call_id=first.tool_call_id,
        source="handoff" if meta.get("kind") == HANDOFF else "takeover",
        reason=str(meta.get("reason") or ""),
        holder=str(meta.get("holder") or ""),
        host=str(meta.get("host") or ""),
        extra_calls=(
            *(c.tool_call_id for c in controls if c is not first),
            *(c.tool_call_id for c in others if c.tool_name != ASK_USER),
        ),
        denied_approvals=tuple(p.tool_call_id for p in output.approvals),
        extra_asks=tuple(c.tool_call_id for c in others if c.tool_name == ASK_USER),
        since=str(meta.get("since") or ""),
    )
