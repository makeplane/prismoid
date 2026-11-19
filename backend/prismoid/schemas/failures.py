# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Why an attempt died, as a type: `FailureKind`, and the one advice table that turns a kind into the
ladder's direction (retry, fall back, escalate now) and what a person is told when the step stops.

A kind is named once, where the exception is caught, from typed facts (a status code, an exception
class, the browser tool's own verdict); everything downstream reads the kind, never the words of the
error. `ModelIncapable(kind=...)` takes its ladder flags from this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

# How a guarded step's next attempt takes up a dead run on its call log: re-entering it where a call
# was cut, or continuing it with one line saying why it stopped.
type Pickup = Literal["reenter", "continue"]


class FailureKind(StrEnum):
    MODEL_FAILED = "model_failed"  # the model missed its output shape or spent its request budget
    REQUEST_REFUSED = "request_refused"  # a 400/413 or a content filter: the request itself
    KEY_REFUSED = "key_refused"  # 401/403 from every model tried: no model ran
    PROVIDER_DOWN = "provider_down"  # an outage, a dropped stream, every fallback candidate failed
    SERVICE_DOWN = "service_down"  # the sandbox or a service the run relies on stopped answering
    WALL_STOP = "wall_stop"  # an express answer ran past its wall clock
    SITE_UNREACHABLE = "site_unreachable"  # the site cannot be reached from the computer
    SITE_REFUSES_BROWSER = "site_refuses_browser"  # the site resets automated browsers
    SITE_CHALLENGED = "site_challenged"  # its bot check needs hosts the sandbox refuses


NARROWER: Final = "Re-run with a narrower goal, or attach a source that contains what you are asking for."
_AGAIN: Final = "Run it again: a fresh attempt often gets through, and nothing was handed over this time."
_KEY: Final = (
    "The model provider refused this deployment's API key (401/403 from every model the step "
    "tried), so no model ever ran and nothing about the goal or its sources was tested. Check the "
    "provider key the deployment is configured with - a wrong or revoked key, or one without access "
    "to these models - then run the task again."
)
_UNREACHABLE: Final = (
    "The site could not be reached from the computer. Check the address is right and the site is "
    "up - a private or internal site needs to be allowed for the task - then ask again."
)
_REFUSES_BROWSER: Final = (
    "The site turns away automated browsers (it reset the connection), so the computer could not "
    "read it. Open it yourself, or name another site or a source to read instead."
)
_CHALLENGED: Final = (
    "The site's page stayed empty: it needs hosts the computer may not reach to load (a bot "
    "check). Open it yourself, or name another site - the store's site for your own region may "
    "not check."
)


@dataclass(frozen=True, slots=True)
class Advice:
    """One kind's direction: the ladder flags it raises with, why a stopped step stopped and what to try,
    both in the person's words."""

    said: str
    suggestions: tuple[str, ...]
    provider: bool = False  # the provider failed, not the model: back off before the next attempt
    refused: bool = False  # the provider would not take the request: never re-sent
    infra: bool = False  # not the model's attempt: it nets out of the model's three
    final: bool = False  # the same wall stops every attempt: escalate now

    @property
    def flags(self) -> dict[str, bool]:
        """The raised flags, as `ModelIncapable.context` carries them."""
        named = {"provider": self.provider, "refused": self.refused, "infra": self.infra, "final": self.final}
        return {flag: True for flag, on in named.items() if on}


ADVICE: Final[dict[FailureKind, Advice]] = {
    FailureKind.MODEL_FAILED: Advice("Every attempt ended before the work was finished", (_AGAIN, NARROWER)),
    FailureKind.REQUEST_REFUSED: Advice(
        "The AI model turned the request down", (_AGAIN, NARROWER), provider=True, refused=True
    ),
    FailureKind.KEY_REFUSED: Advice("The AI provider refused this deployment's key", (_KEY,), provider=True),
    FailureKind.PROVIDER_DOWN: Advice(
        "The AI provider failed or stopped answering", (_AGAIN, NARROWER), provider=True
    ),
    FailureKind.SERVICE_DOWN: Advice(
        "The computer the work runs on stopped answering", (_AGAIN, NARROWER), infra=True
    ),
    FailureKind.WALL_STOP: Advice("The work ran out of time", (_AGAIN, NARROWER), infra=True),
    FailureKind.SITE_UNREACHABLE: Advice(
        "The site could not be reached from the computer", (_UNREACHABLE, NARROWER), final=True
    ),
    FailureKind.SITE_REFUSES_BROWSER: Advice(
        "The site turns away automated browsers", (_REFUSES_BROWSER, NARROWER), final=True
    ),
    FailureKind.SITE_CHALLENGED: Advice(
        "The site's bot check kept its page empty", (_CHALLENGED, NARROWER), final=True
    ),
}
