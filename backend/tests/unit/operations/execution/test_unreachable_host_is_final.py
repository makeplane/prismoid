# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A site the computer cannot reach is a fact about the site, not the model: no second attempt.

"Open https://prismoid-e2e.invalid/ ...": `browser_open` comes back "failed: egress refused - ...
the site could not be reached ... Asking again will not help" and the computer ends `failed`. The
ladder must not buy more attempts (more empty recordings) of the same conversation on the same
model, and the escalation must not say "argued with the brief" or wear the raw agent name."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from prismoid.ai.tools.browser_policy import (
    OpenPlan,
    PageState,
    blanked,
    open_state,
    opened_text,
    page_state,
    refused_state,
    unreachable_target,
)
from prismoid.db.models.plan import PlanStep
from prismoid.errors import ModelIncapable
from prismoid.operations.execution import publish
from prismoid.operations.execution.finish import produce_diagnosis
from prismoid.operations.execution.handlers import produce
from prismoid.operations.execution.handlers.state import Ran
from prismoid.schemas.failures import FailureKind
from prismoid.schemas.outputs import Outcome
from prismoid.schemas.plan import StepState

if TYPE_CHECKING:
    from prismoid.operations.execution.handlers.state import Ladder


def _open(result: dict[str, Any], host: str, *, again: bool = False) -> PageState | None:
    """The facts `browser.open` keeps for one sidecar answer."""
    return open_state(page_state(result), OpenPlan(host=host), again=again)


_DEAD = _open({"ok": False, "error": "egress refused", "blocked": "egress"}, "prismoid-e2e.invalid")
# An Akamai-fronted site (makemytrip.com) can reset the stream on the browser.
# A first reset may be a blip (one more open); a second is the wall.
_RESET = {"ok": False, "error": "navigation failed: net::ERR_HTTP2_PROTOCOL_ERROR", "blocked": "reset"}
_FIRST_RESET = _open(_RESET, "www.makemytrip.com")
_WALL = _open(_RESET, "makemytrip.com", again=True)
_LOADED = _open({"ok": True, "url": "https://example.com/", "title": "Example"}, "example.com")
_UNRESOLVED = {"ok": False, "error": "navigation failed: net::ERR_NAME_NOT_RESOLVED", "blocked": "unresolved"}


def _opened(*facts: PageState | None) -> list[Any]:
    messages: list[Any] = []
    for n, state in enumerate(facts):
        messages.append(
            ModelResponse(parts=[ToolCallPart("browser_open", {"url": "x"}, tool_call_id=f"o{n}")])
        )
        part = ToolReturnPart("browser_open", "failed", tool_call_id=f"o{n}", metadata=state)
        messages.append(ModelRequest(parts=[part]))
    return messages


@pytest.mark.parametrize(
    ("returns", "dead"),
    [
        ((_DEAD,), True),
        ((refused_state("http://10.0.0.8/"),), True),
        ((_open(_UNRESOLVED, "a.example"),), True),
        ((_FIRST_RESET, _WALL), True),  # the site resets the computer's browser: every attempt meets it
        ((_FIRST_RESET,), False),  # one reset may be a flaky CDN: the open is worth once more
        ((_DEAD, _LOADED), False),  # it got somewhere after all
        ((_open({"ok": False, "error": "navigation failed: the page did not load"}, "a.example"),), False),
        ((), False),
    ],
)
def test_the_last_open_says_whether_the_site_is_unreachable(
    returns: tuple[PageState | None, ...], dead: bool
) -> None:
    assert (unreachable_target(_opened(*returns)) is not None) is dead


def _walled(ended: Outcome, messages: list[Any]) -> ModelIncapable:
    died = publish.wall_death(ended, messages)
    assert died is not None
    return died


def test_a_failed_ending_on_an_unreachable_site_is_final() -> None:
    died = _walled(Outcome(status="failed", summary="It could not be reached."), _opened(_DEAD))
    assert died.context.get("final") is True
    assert "could not be reached from here" in str(died)


def test_a_failed_ending_elsewhere_is_no_wall_death() -> None:
    """No site wall: the `failed` ending stands as the step's typed ending, never a retried death."""
    opened = _opened(_LOADED)
    assert (
        publish.wall_death(Outcome(status="failed", summary="The form would not take the date."), opened)
        is None
    )


async def test_a_final_death_escalates_on_its_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    escalated: list[list[str]] = []

    async def escalate(session: Any, task: Any, step: Any, spec: Any, gotchas: list[str], **_: Any) -> None:
        escalated.append(list(gotchas))
        step.transition(StepState.ESCALATED)

    async def commit() -> None:
        return None

    async def refresh(obj: Any, attribute_names: Any = None) -> None:
        return None

    monkeypatch.setattr(produce.finish, "escalate", escalate)
    step = PlanStep(id=uuid.uuid4(), task_id=uuid.uuid4(), ordinal=0, kind="step", title="Open it", spec={})
    step.state, step.attempt, step.infra_attempts, step.infra_failures = StepState.RUNNING.value, 1, 0, 0
    ladder = SimpleNamespace(
        session=SimpleNamespace(commit=commit, refresh=refresh),
        task=SimpleNamespace(is_terminal=lambda: False),
        step=step,
        spec=SimpleNamespace(agent="operator", model_ref=None, lane_role=None),
        contract=SimpleNamespace(output=None),
        gotchas=[],
        final=True,
    )
    with pytest.raises(Exception, match="operator produced no result in 1 attempts"):
        await produce.nothing_published(cast("Ladder", ladder), Ran("", [], None, undelivered="unreachable"))
    assert len(escalated) == 1 and step.step_state is StepState.ESCALATED


def _tried(failure: FailureKind | None) -> list[str]:
    return produce_diagnosis(None, attempts=1, gotchas=["x"], failure=failure).suggested_next


def test_an_unreachable_site_gets_an_honest_what_to_try() -> None:
    tried = _tried(FailureKind.SITE_UNREACHABLE)
    assert any("could not be reached" in line for line in tried)
    assert not any("argued with the brief" in line for line in tried)


@pytest.mark.parametrize(
    ("failure", "heading"),
    [
        (FailureKind.MODEL_FAILED, "Prismoid could not finish this part of the task"),
        (FailureKind.SITE_UNREACHABLE, "Prismoid could not reach the site"),
    ],
)
def test_the_escalation_heading_reads_as_a_sentence(failure: FailureKind, heading: str) -> None:
    last = "- **nothing was published.** the site could not be reached from here, so another attempt cannot help: x"
    diagnosis = produce_diagnosis(None, attempts=1, gotchas=[last], failure=failure)
    assert diagnosis.what_failed == heading
    assert diagnosis.evidence == [last]


def test_a_site_that_refuses_the_browser_says_so_and_never_offers_the_screen() -> None:
    """The model reads what the wall is, so its own ending is honest: a site turning away this
    browser is not down, and handing the person the screen is the same browser on the same
    network - it meets the same wall."""
    said = opened_text("failed: navigation failed", _WALL)
    assert "refuses this browser" in said and "same wall" in said
    died = _walled(Outcome(status="failed", summary="MakeMyTrip did not load."), _opened(_WALL))
    assert died.context.get("final") is True
    assert "refused the computer's browser" in str(died) and "could not be reached from here" in str(died)
    tried = _tried(died.kind)
    assert any("turns away automated browsers" in line for line in tried)
    assert not any("site is up" in line for line in tried)


def test_a_page_its_refused_hosts_left_blank_ends_like_the_bot_wall() -> None:
    """A bot check (as on amazon.com) whose solver host the sandbox refuses leaves
    the page empty on every attempt - one try, final, and the advice says why."""
    blank = {"ok": True, "url": "https://www.amazon.com/", "title": "", "text": ""}
    challenged = page_state(
        blanked(blank, [], [{"host": "a.token.awswaf.com", "reason": "not in allowlist"}])
    )
    looked = [
        *_opened(_LOADED),
        ModelRequest(
            parts=[ToolReturnPart("browser_snapshot", "page", tool_call_id="s", metadata=challenged)]
        ),
    ]
    died = _walled(Outcome(status="failed", summary="Amazon's page stayed blank."), looked)
    assert died.context.get("final") is True
    assert "hosts this sandbox refuses" in str(died)
    tried = _tried(died.kind)
    assert any("bot check" in line for line in tried)
    assert not any("site is up" in line for line in tried)
