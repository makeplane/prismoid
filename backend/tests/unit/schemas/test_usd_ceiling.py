# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`usd_ceiling`: the one reading of a stored task budget, shared by the Bursar and the default cap."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from prismoid.operations.gate_budget import budget_of
from prismoid.operations.task_door import with_default_cap
from prismoid.schemas.usage import cap_is_default, usd_ceiling

CASES: list[tuple[Any, Decimal | None]] = [
    (None, None),
    ({}, None),
    ({"usd": None}, None),
    ({"usd": 0}, None),
    ({"usd": 0.0}, None),
    ({"usd": "0"}, Decimal("0")),
    ({"usd": "0.0000"}, Decimal("0.0000")),
    ({"usd": "2.00"}, Decimal("2.00")),
    ({"usd": 2.5}, Decimal("2.5")),
    ({"tokens": 1000}, None),
    ({"usd": "2.00", "source": "default"}, Decimal("2.00")),  # extra keys are not read
]


@pytest.mark.parametrize(("budget", "expected"), CASES)
def test_usd_ceiling_reads_a_stored_budget(budget: Any, expected: Decimal | None) -> None:
    got = usd_ceiling(budget)
    assert got == expected and (got is None) == (expected is None)
    assert budget_of(budget) == got


@pytest.mark.parametrize(("budget", "expected"), CASES)
def test_the_default_cap_fills_exactly_what_has_no_ceiling(
    budget: Any, expected: Decimal | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.conf import settings

    monkeypatch.setattr(settings.workspaces, "task_default_budget_usd", Decimal("2"))
    out = with_default_cap(budget)
    if expected is None:
        assert out is not None and out["usd"] == "2.00" and cap_is_default(out), "marked as the default"
    else:
        assert out == budget


def test_only_a_marked_cap_is_the_default() -> None:
    assert cap_is_default({"usd": "2.00", "source": "default"})
    assert not cap_is_default({"usd": "2.00"}) and not cap_is_default(None)
