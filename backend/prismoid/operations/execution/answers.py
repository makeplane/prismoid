# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A park's answers: every deferred call of the parked turn, answered from the gate's resolution.

Pure. The resume replays the parked run and hands its ending here (`answer_for`): the park the turn
was is read again off its deferred calls (`classify.deferral_of`), and what comes back is the resumed
segment's logged input. pydantic-ai refuses partial results, so every pending id is resolved: the
call it parked on gets the person's word (an answer, a connect's outcome, a takeover's story), the
calls that shared its turn are told why they did not run, and an approval park's calls get the
gate's one verdict. `resolution` is the park's `resume_state` once its wake has mapped the gate's
resolution onto it (`wake.hydrate_resume`). A park saved before the call log names its ids instead
(`v1_deferral`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.tools import DeferredToolApprovalResult

from prismoid.ai.tools import catalog
from prismoid.ai.tools.browser_policy import act_target
from prismoid.ai.tools.call_rows import declined_text, not_run
from prismoid.operations.execution.caps import (
    APPROVAL_UNMATCHED,
    DENIED_BY_USER,
    ONE_BATCH_AT_A_TIME,
    ONE_CONNECT_AT_A_TIME,
    OUT_OF_APPROVALS,
    OUT_OF_QUESTIONS,
    RETRY_AFTER_ANSWER,
)
from prismoid.operations.execution.classify import deferral_of
from prismoid.operations.execution.deferrals import (
    NOT_RUN_FOR_CONTROL,
    ApprovalDeferral,
    BudgetDeferral,
    ConnectDeferral,
    ControlDeferral,
    Deferral,
    ParkDeferral,
    PauseDeferral,
)
from prismoid.schemas.guard import BUDGET_ASK_AGAIN, BUDGET_CONTINUE, BUDGET_RESUMED
from prismoid.schemas.park import ParkKind, ParkRecord, V1Park
from prismoid.schemas.steering import pause_resume_text

type Resolution = Mapping[str, object]
type Approvals = dict[str, DeferredToolApprovalResult | bool]


def answer_for(
    park: ParkRecord, ending: object, resolution: Resolution, *, reset: str = ""
) -> DeferredToolResults | str:
    """The replayed run's ending, answered: every pending call of the parked turn - or, for a budget
    park that held no call (the model stopped at the cap itself), the one line its run continues
    with. `reset` leads the parked call's answer when the Machine's sleep restarted the browser."""
    if isinstance(ending, DeferredToolRequests):
        return answers_to(deferral_of(park, ending), ending, resolution, reset=reset)
    if park.kind is ParkKind.BUDGET:
        return f"{reset}{BUDGET_CONTINUE}"
    raise TypeError("a resume answers a run that ended on deferred calls")


def answers_to(
    deferral: ParkDeferral, requests: DeferredToolRequests, resolution: Resolution, *, reset: str = ""
) -> DeferredToolResults:
    """Every pending call of the turn `deferral` parked, answered from the gate's resolution."""
    paused = pause_resume_text(
        _paused_by(deferral) or "Someone", str(resolution.get("resumed_by") or "") or None
    )
    match deferral:
        case BudgetDeferral():
            calls = _all(deferral.calls, not_run(f"{reset}{BUDGET_RESUMED}"))
            calls |= _all(deferral.asks, not_run(f"{reset}{BUDGET_ASK_AGAIN}"))
            return DeferredToolResults(
                calls=calls, approvals=_all(deferral.approvals, ToolDenied(f"{reset}{BUDGET_RESUMED}"))
            )
        case ApprovalDeferral():
            return DeferredToolResults(approvals=verdicts(deferral, requests, resolution))
        case PauseDeferral():
            calls = _all(deferral.calls, paused) | _all(deferral.extra_asks, ONE_BATCH_AT_A_TIME)
            return DeferredToolResults(
                calls=calls, approvals=_all(deferral.denied_approvals, ToolDenied(paused))
            )
        case ControlDeferral():
            held = str(resolution.get("held_text") or NOT_RUN_FOR_CONTROL)
            calls = _all((*deferral.extra_calls, *deferral.extra_asks), held) | _all(
                deferral.paused_calls, paused
            )
            calls[deferral.tool_call_id] = f"{reset}{resolution.get('answer', OUT_OF_QUESTIONS)}"
            return DeferredToolResults(
                calls=calls, approvals=_all(deferral.denied_approvals, ToolDenied(held))
            )
        case ConnectDeferral() | Deferral():
            calls = _all(deferral.extra_calls, ONE_CONNECT_AT_A_TIME) | _all(
                deferral.extra_asks, ONE_BATCH_AT_A_TIME
            )
            calls |= _all(deferral.paused_calls, paused)
            # A connect answered in place says so (`answer`); a gate's resolution says what happened.
            said = connect_result(resolution) if isinstance(deferral, ConnectDeferral) else OUT_OF_QUESTIONS
            own = resolution.get("answer", said)
            calls[deferral.tool_call_id] = f"{reset}{own}"
            return DeferredToolResults(
                calls=calls, approvals=_all(deferral.denied_approvals, ToolDenied(RETRY_AFTER_ANSWER))
            )


def verdicts(deferral: ApprovalDeferral, requests: DeferredToolRequests, resolution: Resolution) -> Approvals:
    """An approval park's verdict for each of its calls. A decline (or a spent budget's
    `deny_overflow`) denies all alike; a steer that closed the gate holds all. An accept approves,
    and an acting browser call (one the toolset fingerprinted) is approved with its ORIGINAL args, as
    the replayed run made the call, plus the exact `expect` the approver looked at: never a fresh
    inspect, never a model-suppliable value. A fingerprinted call whose part is not among the
    deferred calls under its id and tool name is denied, never approved with `expect` alone."""
    action = str(resolution.get("approval_action", "decline"))
    ids = [call_id for call_id, _, _, _ in deferral.items]
    if action == "superseded":
        return _all(ids, ToolDenied(str(resolution["superseded_text"])))
    if action == "deny_overflow":
        return _all(ids, ToolDenied(OUT_OF_APPROVALS))
    if action != "accept":
        return {
            call_id: _declined(deferral, call_id, name, tool) for call_id, name, tool, _ in deferral.items
        }
    parts = {p.tool_call_id: p for p in (*requests.approvals, *requests.calls)}
    return {
        call_id: _approved(parts.get(call_id), tool, deferral.fingerprints.get(call_id))
        for call_id, _, tool, _ in deferral.items
    }


def _declined(deferral: ApprovalDeferral, call_id: str, name: str, tool: str) -> ToolDenied:
    """A person's Deny of one call, marked with the act it declined where the tool's name alone
    does not say (`declined_text`): a fingerprinted browser act's targets, a browser open's host."""
    fingerprint = deferral.fingerprints.get(call_id)
    if isinstance(fingerprint, dict):
        return ToolDenied(declined_text(act_target(fingerprint)))
    if tool == "browser.open":
        return ToolDenied(declined_text(name))
    return ToolDenied(DENIED_BY_USER)


def _approved(part: ToolCallPart | None, tool: str, fingerprint: object) -> DeferredToolApprovalResult:
    """One accepted call: bare, or with its original args and the approved fingerprint."""
    if not isinstance(fingerprint, dict):
        return ToolApproved()
    if part is None or part.tool_name != catalog.model_name(tool):
        return ToolDenied(APPROVAL_UNMATCHED)
    return ToolApproved(override_args={**part.args_as_dict(), "expect": fingerprint})


def _all[V](ids: Iterable[str], value: V) -> dict[str, V]:
    return dict.fromkeys(ids, value)


def _paused_by(deferral: ParkDeferral) -> str:
    return deferral.paused_by if not isinstance(deferral, ApprovalDeferral | BudgetDeferral) else ""


def connect_result(resolution: Resolution) -> str:
    """The connect channel: the parked `request_connector` call's result is what actually happened -
    connected and attached, accepted but unattached, or declined."""
    meta = resolution.get("connect")
    meta = meta if isinstance(meta, dict) else {}
    slug = str(meta.get("slug") or "")
    display = str(meta.get("display") or meta.get("service") or "the service")
    verdict = str(resolution.get("connect_result", "declined"))
    if verdict == "connected":
        return (
            f"Connected: the user connected {display} and it is attached to "
            f"this task - its tools (prefixed `{slug}_`) are available now; "
            "use them directly."
        )
    if verdict == "accepted_unattached":
        return (
            f"The user accepted, but {display} is not attached and live on "
            "this task - continue without it and say what was skipped."
        )
    return f"The user chose not to connect {display} - continue without it and say what was skipped."


def v1_requests(history: list[ModelMessage]) -> DeferredToolRequests:
    """A v1 park's deferred calls: the calls its saved conversation ended on."""
    last = next((m for m in reversed(history) if isinstance(m, ModelResponse)), None)
    pending = [p for p in (last.parts if last is not None else []) if isinstance(p, ToolCallPart)]
    return DeferredToolRequests(approvals=pending)


def v1_deferral(v1: V1Park) -> ParkDeferral | None:
    """The turn a v1 park parked, from the ids it saved; None for a budget park that held no call."""
    record, own = v1.record, v1.own or ""
    match record.kind:
        case ParkKind.BUDGET:
            held = (v1.budget_calls, v1.budget_asks, v1.budget_approvals)
            if not any(held):
                return None
            return BudgetDeferral(calls=v1.budget_calls, asks=v1.budget_asks, approvals=v1.budget_approvals)
        case ParkKind.APPROVAL:
            return ApprovalDeferral(
                items=[
                    (c, str(m.get("connector") or ""), str(m.get("tool") or ""), "")
                    for c, m in v1.approval_calls.items()
                ],
                fingerprints={c: _fingerprint(m) for c, m in v1.approval_calls.items()},
            )
        case ParkKind.PAUSE:
            return PauseDeferral(
                calls=v1.paused_calls,
                denied_approvals=v1.denied_approvals,
                paused_by=record.paused_by,
                extra_asks=v1.extra_asks,
            )
        case ParkKind.CONTROL:
            return ControlDeferral(
                tool_call_id=own,
                source="",
                reason="",
                holder="",
                extra_calls=v1.held_calls,
                denied_approvals=v1.held_approvals,
                paused_calls=v1.paused_calls,
                paused_by=record.paused_by,
            )
        case ParkKind.CONNECT:
            return ConnectDeferral(
                tool_call_id=own,
                service="",
                why="",
                extra_calls=v1.extra_calls,
                denied_approvals=v1.denied_approvals,
                extra_asks=v1.extra_asks,
                paused_calls=v1.paused_calls,
                paused_by=record.paused_by,
            )
        case ParkKind.QUESTION:
            return Deferral(
                tool_call_id=own,
                questions=[],
                denied_approvals=v1.denied_approvals,
                extra_calls=v1.extra_calls,
                extra_asks=v1.extra_asks,
                paused_calls=v1.paused_calls,
                paused_by=record.paused_by,
            )


def _fingerprint(meta: Mapping[str, Any]) -> dict[str, str] | None:
    found = meta.get("fingerprint")
    return found if isinstance(found, dict) else None
