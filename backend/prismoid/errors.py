# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The domain's exceptions. One definition, mapped to HTTP in exactly one place (`api/errors.py`).

They live at the root, below `db`, because every layer raises them and no layer should have to
import a higher one to do it. `api/errors.py` owns the *only* translation into status codes
(the error envelope); nothing else in the system knows what a 409 is.
"""

from __future__ import annotations

from prismoid.schemas.failures import ADVICE, FailureKind, Pickup


class PrismoidError(Exception):
    """Base. Carries a stable machine-readable `code` - the envelope's `error.code`."""

    code = "internal"

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.context = context


class NotFound(PrismoidError):
    """404. Also what a scope failure raises when *existence itself* is privileged - a 403
    on a project you cannot see would tell you the project exists."""

    code = "not_found"


class NoMachine(NotFound):
    """404 `no_machine` - the task has no Machine yet: nothing to sleep or reset."""

    code = "no_machine"


class BadRequest(PrismoidError):
    """400 - a request that cannot mean anything from this caller (switching the workspace of a
    login session with an API key, which has no session to switch)."""

    code = "bad_request"


class Unauthorized(PrismoidError):
    """401 - no valid Actor."""

    code = "unauthorized"


class Forbidden(PrismoidError):
    """403 - a valid Actor, insufficient role."""

    code = "forbidden"


class WorkspaceCapReached(Forbidden):
    """403 `workspace_cap` - this person has founded as many team workspaces as this deployment allows."""

    code = "workspace_cap"


class SignupDisabled(Forbidden):
    """403 `signup_disabled` - open signup is off here and no invite was offered."""

    code = "signup_disabled"


class AllowanceSpent(PrismoidError):
    """402 `allowance_spent` - the workspace has spent its allowance where this deployment sets one
    (`operations/allowance.py`, ADR-103): new work is refused at the door, with the same words
    wherever it knocks. A distribution passes its own words for its own allowance."""

    code = "allowance_spent"
    COPY = "This workspace has reached its spending allowance."

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message or self.COPY, **context)


class InvalidTransition(PrismoidError):
    """409. The task state machine is the spec; this is it refusing to be bypassed."""

    code = "invalid_transition"


class BudgetExceeded(PrismoidError):
    """409 `budget_exceeded`. Never a silent overrun - the Bursar opens a gate instead."""

    code = "budget_exceeded"


class PolicyDenied(PrismoidError):
    """403. A door said no: an un-allowlisted domain, a path outside the agent's folders."""

    code = "policy_denied"


class PathRefused(PolicyDenied):
    """A path the frame's file door refused (sandbar's 400 for an unclean path, its 403
    `forbidden_path`, or the tool's own root check). The file tools hand it to the model as a
    result; a caller that lets one escape (harvest) re-words it. The subclass tells a refused path
    from a bare `PolicyDenied` (a sandbox gone, a tool not granted), which the tools do not catch."""


class ProviderUnavailable(PolicyDenied):
    """A catalog row whose provider cannot be built on this host: no key for a provider that needs
    one, or its SDK refused to construct. Raised by `gateway.providers.build_model` alone, with
    the row's `ref` in its context, so `resolve` can skip the row - a family down costs a
    candidate, never the step."""


class ModelMisconfigured(PrismoidError):
    """A catalog row that no host could build: a protocol nobody knows, a media protocol in a chat
    seat, an OpenAI-compatible row without its base URL. It is the operator's data that is wrong,
    not a refusal of the caller, so it is not a `PolicyDenied`. Raised by
    `gateway.providers.build_model` with the row's `ref` in its context; `resolve` skips the row
    the way it skips one this host holds no key for, so one bad row costs a candidate, never the
    step."""

    code = "model_misconfigured"


class HostFull(PolicyDenied):
    """A provider's `quota_exceeded`: no host has room for one more awake computer. Room comes back
    when another computer sleeps, so a caller that can wait waits, and everyone else sees the
    `PolicyDenied` it always was."""


class StepFailed(PrismoidError):
    """A step ended without its work: its agent could not do it, was blocked, or produced nothing in
    its attempts. Never a retry; the message is the agent's explanation, written for the person."""

    code = "step_failed"


class CouldNotDo(StepFailed):
    """A step whose agent ended on a typed stop of its own: a refusal, a wall, a gap in its tools. The
    task still fails, and its ending reads `could_not` rather than a system fault."""

    code = "could_not_do"


class ValidationFailed(PrismoidError):
    """422 with `fields` - a QuestionSet answer that doesn't match its schema, a skill body over
    budget."""

    code = "validation_failed"

    def __init__(self, message: str = "", fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


class FolderRefused(ValidationFailed):
    """422 `folder_refused` - a folder that may not be granted to a task on the device: `/`, the
    home, a system folder, or one that holds or sits in a secret."""

    code = "folder_refused"


class FeatureDisabled(PrismoidError):
    """409 `feature_disabled` - fleet mode with the flag off."""

    code = "feature_disabled"


class Conflict(PrismoidError):
    """409 - a publish that would clobber, a skill publish with red evals."""

    code = "conflict"


class StaleSteer(Conflict):
    """409 - a steer issued against an older plan epoch. `missed` lists what its author
    had not seen, oldest first; the handler renders it into the envelope."""

    code = "stale_steer"

    def __init__(self, message: str, missed: list[dict[str, object]]) -> None:
        super().__init__(message)
        self.missed = missed


class DeviceTaskPrivate(Conflict):
    """409 `device_task_private` - a task on the device and a Project with members besides its
    owner never meet: not by create, by move, or by adding a member."""

    code = "device_task_private"


class FolderBusy(Conflict):
    """409 `folder_busy` - another live task works in that folder."""

    code = "folder_busy"


class PayloadTooLarge(PrismoidError):
    """413 - an inbound body over its published cap (the routine fire door's payload).
    Refused loudly at the door, never silently truncated: Devin trims at 200 KB and callers
    discover it in production; a 413 with the cap in the message is the honest contract."""

    code = "payload_too_large"


class Unavailable(PrismoidError):
    """503 - a part of the service cannot work right now and the detail is not the caller's (a
    local key file that fails its checks). The cause is logged, never put in the envelope."""

    code = "unavailable"


class RateLimited(PrismoidError):
    """429 - too many requests. Carries `retry_after` seconds so a client backs off instead of hammering;
    the handler also sets the `Retry-After` header from it. Login stuffing and a runaway client
    are the same shape to the server, and both get told, precisely, when to come back."""

    code = "rate_limited"

    def __init__(self, message: str = "", *, retry_after: int = 60, **context: object) -> None:
        super().__init__(message, **context)
        self.retry_after = retry_after


class ModelIncapable(PrismoidError):
    """A model answered (a clean 200) but could not produce the asked-for shape: not an outage, so the
    caller decides whether to fall through. `pickup`, `provider`, `refused`, `kind`: how a retry resumes."""

    code = "model_incapable"

    def __init__(
        self,
        message: str = "",
        *,
        pickup: Pickup | None = None,
        kind: FailureKind | None = None,
        **context: object,
    ) -> None:
        super().__init__(message, **{**(ADVICE[kind].flags if kind else {}), **context})
        self.pickup: Pickup | None = pickup
        self.kind = kind


class NotImplementedYet(PrismoidError):
    """501. A named stub that says which milestone implements it.

    It exists so that `api/urls.py` is the complete route table - the route table is only a
    complete picture if it is *whole*. The gate is `grep -rc "501" prismoid/api` → 0: the stubs are
    a countdown, and the count is checkable.
    """

    code = "not_implemented"


class TenantUnbound(PrismoidError):
    """A tenant-scoped read or write ran with no workspace bound to its unit of work
    (see ADR-061). Always a bug in a door or a job entry point, never a user error: a 500, logged
    loudly, and never a leak."""

    code = "tenant_unbound"
