# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The error envelope. The ONLY place in the system that knows what a status code is.

Every non-2xx body in the product is exactly:

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

One shape, always - so a client writes one error path, and `request_id` is the thread from the
message a user pastes to the span in Logfire that explains it.

Unhandled exceptions become a 500 whose body says `internal` and nothing else. The detail is
logged with the request_id; it never crosses the wire. A stack trace in a response body is a
gift to whoever is probing you.
"""

from __future__ import annotations

from typing import Any

import logfire
import orjson
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from prismoid.errors import (
    AllowanceSpent,
    BadRequest,
    BudgetExceeded,
    Conflict,
    DeviceTaskPrivate,
    FeatureDisabled,
    FolderBusy,
    FolderRefused,
    Forbidden,
    InvalidTransition,
    NoMachine,
    NotFound,
    PayloadTooLarge,
    PolicyDenied,
    PrismoidError,
    RateLimited,
    SignupDisabled,
    StaleSteer,
    Unauthorized,
    Unavailable,
    ValidationFailed,
    WorkspaceCapReached,
)

# The mapping table, and it is the whole of it.
STATUS: dict[type[PrismoidError], int] = {
    BadRequest: 400,
    NotFound: 404,
    NoMachine: 404,
    Unauthorized: 401,
    AllowanceSpent: 402,
    Forbidden: 403,
    SignupDisabled: 403,  # its own row: the lookup is by exact type
    WorkspaceCapReached: 403,
    PolicyDenied: 403,
    InvalidTransition: 409,
    BudgetExceeded: 409,
    FeatureDisabled: 409,
    Conflict: 409,
    StaleSteer: 409,  # its own row: the lookup is by exact type
    DeviceTaskPrivate: 409,
    FolderBusy: 409,
    ValidationFailed: 422,
    FolderRefused: 422,
    PayloadTooLarge: 413,
    RateLimited: 429,
    Unavailable: 503,
    # No not-implemented row: no route raises NotImplementedYet - the named-stub countdown
    # is at zero. If a future stub needs one, the class still lives in prismoid.errors; re-add its row.
}


class ErrorResponse(Response):
    """The envelope, rendered with orjson.

    2xx bodies do not come through here: FastAPI serialises a typed return straight to JSON bytes
    via Pydantic, which is the fast path and is faster than a custom response class.
    Errors are hand-built dicts, so they need a renderer - and it may as well be the fast one.
    """

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content)


def envelope(code: str, message: str, request_id: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    body.update(extra)
    return {"error": body}


def request_id_of(request: Request) -> str:
    return str(getattr(request.state, "request_id", "-"))


async def handle_domain_error(request: Request, exc: Exception) -> ErrorResponse:
    assert isinstance(exc, PrismoidError)
    status = STATUS.get(type(exc), 500)
    extra: dict[str, Any] = {}
    headers: dict[str, str] = {}
    if isinstance(exc, ValidationFailed) and exc.fields:
        extra["fields"] = exc.fields
    if isinstance(exc, StaleSteer):
        extra["missed"] = exc.missed  # what the steer's author had not seen
    if isinstance(exc, RateLimited):
        extra["retry_after"] = exc.retry_after  # in the envelope for JSON clients…
        headers["Retry-After"] = str(exc.retry_after)  # …and the header the HTTP spec defines (429)
    return ErrorResponse(
        status_code=status,
        content=envelope(exc.code, exc.message, request_id_of(request), **extra),
        headers=headers or None,
    )


async def handle_request_validation(request: Request, exc: Exception) -> ErrorResponse:
    """FastAPI's own 422 - reshaped into our envelope, with `fields` a client can render."""
    assert isinstance(exc, RequestValidationError)
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p not in ("body", "query", "path"))
        fields[loc or "body"] = str(err["msg"])
    return ErrorResponse(
        status_code=422,
        content=envelope(
            "validation_failed", "the request body is not valid", request_id_of(request), fields=fields
        ),
    )


async def handle_unexpected(request: Request, exc: Exception) -> ErrorResponse:
    """Anything we did not name. Logged in full, revealed not at all."""
    rid = request_id_of(request)
    logfire.exception("unhandled error", request_id=rid, path=request.url.path)
    return ErrorResponse(
        status_code=500,
        content=envelope("internal", "an unexpected error occurred", rid),
    )


def install(app: FastAPI) -> None:
    app.add_exception_handler(PrismoidError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_request_validation)
    app.add_exception_handler(Exception, handle_unexpected)
