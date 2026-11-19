# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The run guard speaks to the agent it rides: only the operator ends with `done(Outcome)`, so only
the operator is told to call `done`. A writer or a coder told that would call a tool it does not
hold; its stall words say finish with what it has."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel

import prismoid.ai.agents.base as agents_base
from prismoid.ai.agents.base import build
from prismoid.ai.capabilities.loops import RunGuard
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.guard import FINISH_NOW, FINISH_NOW_ANSWER, proof_nudge, stall_notice
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit


def _guard(name: str, monkeypatch: pytest.MonkeyPatch) -> RunGuard:
    monkeypatch.setattr(
        agents_base, "resolve", lambda *a, **k: FunctionModel(lambda m, i: ModelResponse(parts=[]))
    )
    catalog = [
        ModelSpec(
            provider="fake",
            model_id="m1",
            family=Family("fake"),
            protocol="openai_compatible",
            classes=tuple(ModelClass),
            base_url="https://example.invalid/v1",
            prices={Unit.INPUT_TOKEN: Decimal("0")},
        )
    ]
    agent = build(name, TaskDeps(), catalog).agent
    (found,) = [c for c in agent.root_capability.capabilities if isinstance(c, RunGuard)]
    return found


def test_the_computer_is_told_done_and_the_writer_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _guard("operator", monkeypatch).outcome
    assert not _guard("writer", monkeypatch).outcome
    assert not _guard("maker", monkeypatch).outcome


def test_the_answer_words_never_name_done() -> None:
    assert "done(" in stall_notice("browser_click") and "done(" in FINISH_NOW
    assert "done(" not in stall_notice("shell", outcome=False)
    assert "done(" not in FINISH_NOW_ANSWER


def test_told_to_finish_an_answering_agent_is_told_to_publish_what_it_built() -> None:
    assert "publish what you built" in FINISH_NOW_ANSWER


def test_a_failed_proof_says_how_to_quote_the_page() -> None:
    """Live, a price lookup quoted 'up to 12 seats' where the page said '12 users', and its one more
    turn paraphrased again: the nudge says to copy the words from a fresh look at the named page."""
    said = proof_nudge("'up to 12 seats' is not on the page now")
    assert "exactly" in said and "snapshot" in said and "paraphrase" in said
