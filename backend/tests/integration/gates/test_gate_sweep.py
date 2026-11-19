# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The TTL sweep resolves an expired gate by its own `on_timeout` policy.

A gate with an expiry and no sweeper is decoration: the task parks and waits forever. This proves the
three policies (`proceed_with_defaults`, `cancel`, `wait`) each do the right thing, through the same
`resolve` path a human uses - a timeout is the machine typing the answer the policy chose, not a
second way to resolve a gate.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.session import session_scope
from prismoid.operations import gates as gate_ops
from prismoid.operations import questions
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import (
    Clarification,
    GateAction,
    GateKind,
    GateSpec,
    GateState,
    ProposedQuestion,
    Question,
    QuestionOption,
    QuestionSet,
    QuestionType,
    TimeoutPolicy,
)
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def computer_use_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # The TTL arms only: with the switch off, the sweep's kill-switch arm would also
    # decline every open CONTROL and browser gate in the database - other workers' tests' included.
    monkeypatch.setattr(settings.computer_use, "enabled", True)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


def _question_set(policy: TimeoutPolicy, *, expired: bool) -> QuestionSet:
    when = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=-30 if expired else 3600)
    return QuestionSet(
        title="A few things",
        questions=[
            Question(
                id="q1",
                prompt="Currency?",
                type=QuestionType.SINGLE_SELECT,
                options=[QuestionOption(value="eur", label="eur"), QuestionOption(value="usd", label="usd")],
                required=False,
                default="usd",
            )
        ],
        expires_at=when,
        on_timeout=policy,
    )


async def _park(
    db: AsyncSession, actor: Actor, policy: TimeoutPolicy, *, expired: bool, qs: QuestionSet | None = None
) -> Gate:
    qs = qs or _question_set(policy, expired=expired)
    task = await task_ops.create_task(db, actor, goal="g")
    gate = Gate(
        task_id=task.id,
        kind=GateKind.INPUT.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.INPUT, title="A few things", questions=qs).model_dump(mode="json"),
        expires_at=qs.expires_at,
    )
    db.add(gate)
    await db.commit()
    return gate


async def test_proceed_with_defaults_declines_and_uses_the_default(db: AsyncSession, actor: Actor) -> None:
    gate = await _park(db, actor, TimeoutPolicy.PROCEED_WITH_DEFAULTS, expired=True)

    swept = await gate_ops.sweep(db)

    assert gate.id in swept
    refreshed = await db.get(Gate, gate.id)
    assert refreshed is not None and refreshed.resolution is not None
    assert refreshed.state == GateState.RESOLVED.value
    assert refreshed.resolution["action"] == GateAction.DECLINE.value
    assert refreshed.resolution["content"] == {"q1": "usd"}, "decline must proceed on the default"


async def test_a_confirm_the_model_defaulted_to_yes_declines_as_no(db: AsyncSession, actor: Actor) -> None:
    confirm = ProposedQuestion(prompt="Send Rs 20,000 now?", type=QuestionType.BOOLEAN, default="true")
    asked = questions.build(
        Clarification(questions=[confirm]), now=dt.datetime.now(dt.UTC), ttl=dt.timedelta(seconds=-30)
    )
    assert asked is not None
    gate = await _park(db, actor, TimeoutPolicy.PROCEED_WITH_DEFAULTS, expired=True, qs=asked)

    assert gate.id in await gate_ops.sweep(db)
    refreshed = await db.get(Gate, gate.id)
    assert refreshed is not None and refreshed.resolution is not None
    assert refreshed.resolution["content"] == {"q1": False}


async def test_cancel_policy_cancels_the_gate(db: AsyncSession, actor: Actor) -> None:
    gate = await _park(db, actor, TimeoutPolicy.CANCEL, expired=True)

    swept = await gate_ops.sweep(db)

    assert gate.id in swept
    refreshed = await db.get(Gate, gate.id)
    assert refreshed is not None and refreshed.resolution is not None
    assert refreshed.resolution["action"] == GateAction.CANCEL.value


async def test_wait_policy_leaves_the_gate_open(db: AsyncSession, actor: Actor) -> None:
    """`wait` means only a human is an acceptable answer - the expiry does not apply."""
    gate = await _park(db, actor, TimeoutPolicy.WAIT, expired=True)

    swept = await gate_ops.sweep(db)

    assert gate.id not in swept
    refreshed = await db.get(Gate, gate.id)
    assert refreshed is not None and refreshed.state == GateState.OPEN.value


async def test_a_gate_that_has_not_expired_is_left_alone(db: AsyncSession, actor: Actor) -> None:
    gate = await _park(db, actor, TimeoutPolicy.PROCEED_WITH_DEFAULTS, expired=False)

    swept = await gate_ops.sweep(db)

    assert gate.id not in swept
    refreshed = await db.get(Gate, gate.id)
    assert refreshed is not None and refreshed.state == GateState.OPEN.value
