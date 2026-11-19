# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How a failed agent run reads to the retry ladder: each failure `_run` catches becomes one
`ModelIncapable` with its reason (`{exc}` in a message is the failure as the library says it) and its kind, whose
flags (`schemas.failures.ADVICE`) steer the ladder. Pure: the exceptions are built by hand."""

from __future__ import annotations

import httpx
import pytest
from pydantic_ai.exceptions import (
    ContentFilterError,
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)

from prismoid.ai.agents.run import model_incapable
from prismoid.ai.frames.driver import FrameUnreachable
from prismoid.schemas.failures import ADVICE, FailureKind

BUDGET = (
    "the run exceeded its request budget ({exc}) - do the work in FEWER, larger steps: batch your "
    "file reads, build once, verify ONCE, publish"
)


@pytest.mark.parametrize(
    ("exc", "guarded", "message", "kind"),
    [
        (UnexpectedModelBehavior("bad tool args"), False, "bad tool args", FailureKind.MODEL_FAILED),
        (
            ContentFilterError("Content filter triggered. Finish reason: 'refusal'"),
            True,
            "the provider's content filter refused the request: Content filter triggered. Finish reason: 'refusal'",
            FailureKind.REQUEST_REFUSED,
        ),
        (
            ModelAPIError("m", "reset"),
            False,
            "the provider connection failed mid-run: reset",
            FailureKind.PROVIDER_DOWN,
        ),
        (
            ModelHTTPError(413, "m", "too long"),
            True,
            "the provider connection failed mid-run: status_code: 413, model_name: m, body: too long",
            FailureKind.REQUEST_REFUSED,
        ),
        (
            ModelHTTPError(500, "m"),
            False,
            "the provider connection failed mid-run: status_code: 500, model_name: m, body: None",
            FailureKind.PROVIDER_DOWN,
        ),
        (
            FallbackExceptionGroup(
                "all failed", [ValueError("a"), ValueError("b"), ValueError("c"), ValueError("d")]
            ),
            False,
            "no model could take the call - a; b; c",
            FailureKind.PROVIDER_DOWN,
        ),
        (
            httpx.ReadError("429 inside the stream"),
            False,
            "the provider connection failed mid-run: 429 inside the stream",
            FailureKind.PROVIDER_DOWN,
        ),
        (
            UsageLimitExceeded("over 50"),
            True,
            "the run hit its request ceiling ({exc}); continue from where you stopped",
            FailureKind.MODEL_FAILED,
        ),
        (UsageLimitExceeded("over 50"), False, BUDGET, FailureKind.MODEL_FAILED),
        (
            FrameUnreachable("the frame did not answer exec"),
            True,
            "the sandbox or a service the run relies on stopped answering: the frame did not answer exec",
            FailureKind.SERVICE_DOWN,
        ),
        (
            ConnectionError("the connector closed the connection"),
            False,
            "the sandbox or a service the run relies on stopped answering: the connector closed the connection",
            FailureKind.SERVICE_DOWN,
        ),
    ],
)
def test_each_failure_reads_as_one_attempt_death(
    exc: Exception, guarded: bool, message: str, kind: FailureKind
) -> None:
    incapable = model_incapable(exc, guarded=guarded)
    assert str(incapable) == message.format(exc=exc)
    assert incapable.kind is kind and incapable.context == ADVICE[kind].flags
