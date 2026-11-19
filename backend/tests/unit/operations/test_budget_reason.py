# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The budget gate's reason says money the way a person reads it; its figures stay exact.

Never "the task budget of $0.40 is spent ($0.000254100000 so far)" -
the ledger's twelve decimals pasted into a sentence."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from prismoid.ai.rules.budget import BudgetVerdict
from prismoid.operations import gate_budget
from prismoid.schemas.guard import budget_notice
from prismoid.schemas.plan import BudgetFigures, said_usd

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from prismoid.db.models.task import Task


@pytest.mark.parametrize(
    ("amount", "said"),
    [
        ("0.000254100000", "$0.0002541"),
        ("0.0049999", "$0.005"),
        ("0.123456", "$0.12"),
        ("10", "$10.00"),
        ("4.005", "$4.01"),
        ("0", "$0.00"),
        ("0.00000001", "$0.00000001"),
    ],
)
def test_money_reads_in_cents_or_four_significant_figures(amount: str, said: str) -> None:
    assert said_usd(Decimal(amount)) == said


def test_the_run_guard_notice_says_money_the_same_way() -> None:
    """The guard's 50% notice on a tiny cap reads the cap, never "$0.00 of $0.00"."""
    notice = budget_notice(BudgetFigures(ceiling_usd=Decimal("0.002"), spent_usd=Decimal("0.0011")))
    assert notice is not None and "($0.0011 of $0.002)" in notice.text


async def test_the_gate_reason_rounds_and_the_figures_stay_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: dict[str, Any] = {}

    async def open_budget_gate(session: Any, task: Any, **kwargs: Any) -> Any:
        opened.update(kwargs)
        return None

    monkeypatch.setattr(gate_budget, "open_budget_gate", open_budget_gate)
    spent = Decimal("0.000254100000")
    verdict = BudgetVerdict(spent=spent, limit=Decimal("0.40"), scope="task")
    await gate_budget.open_budget(
        cast("AsyncSession", None), cast("Task", SimpleNamespace(id=uuid.uuid4())), verdict, None
    )
    assert (
        opened["reason"]
        == "the task budget of $0.40 is spent ($0.0002541 so far); raise it and resume, or cancel"
    )
    assert opened["budget_figures"].spent_usd == spent
