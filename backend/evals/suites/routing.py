# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`just evals --suite routing` - do the builtins route?

Every builtin carries routing cases: a goal, and the skill it should load. This suite runs each case
through the **live Lead**, decision-only, against the index of the builtins (`skill_selection.loaded`, the
publish gate's own decision), and reports how often the skill it loads is the one the case expected. It is
the number that says whether the descriptions are sharp - the Lead is only as good as the "Load when …
Not for …" triggers it reads, so a miss here is a description to fix, not a model to blame.

Precision and recall are per-skill: **recall** is "of the goals that should load `report`, how many
did", and **precision** is "of the goals the Lead sent to `report`, how many belonged there". A
skill can have perfect recall and poor precision if it is a magnet that steals other skills' goals -
which is exactly the failure a single accuracy number hides, and why both are reported.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prismoid.operations.skill_selection import Index


@dataclass
class Case:
    skill: str
    goal: str
    expect: str
    chosen: str = ""

    @property
    def ok(self) -> bool:
        return self.chosen == self.expect


async def _load_cases_and_index() -> tuple[list[Case], Index]:
    """The builtins' cases and the index of the builtins alone: user skills are a production concern,
    not a measure of the builtins' sharpness."""
    from prismoid.db.session import session_scope
    from prismoid.operations import skill_selection
    from prismoid.operations import skills as ops
    from prismoid.testutils.tenancy import member

    # Read every field inside the scope: `session_scope` commits on exit and expires the instances, so
    # touching `h.origin` afterwards raises `DetachedInstanceError`. Materialise plain values first.
    async with session_scope() as session:
        # A skill read resolves inside a workspace (ADR-061: its own versions + the platform
        # builtins). A scratch, empty one reads exactly the builtins; the scope's rollback drops it.
        await member(session, "routing-evals")
        builtins = [
            (h.name, list((h.manifest or {}).get("cases", [])))
            for h in await ops.list_skills(session)
            if h.origin == "builtin"
        ]
        index = await skill_selection.index_with(session)

    names = {name for name, _ in builtins}
    index = skill_selection.Index(
        needs={n: t for n, t in index.needs.items() if n in names},
        described={n: d for n, d in index.described.items() if n in names},
        menu=tuple(card for card in index.menu if card.name in names),
    )
    cases = [
        Case(skill=name, goal=str(c["goal"]), expect=str(c["expect"])) for name, raw in builtins for c in raw
    ]
    return cases, index


async def _route_all(cases: list[Case], index: Index, concurrency: int) -> None:
    from prismoid.db.session import session_scope
    from prismoid.operations import skill_selection

    gate = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> None:
        # Each case is independent; a fresh session per call keeps them concurrent without sharing a
        # connection (the Lead's call is a model round-trip, journalled as spend).
        async with gate, session_scope() as session:
            case.chosen = await skill_selection.loaded(session, case.goal, index)

    await asyncio.gather(*(one(c) for c in cases))


def _report(cases: list[Case]) -> float:
    per_expected: dict[str, list[Case]] = defaultdict(list)  # by the skill that SHOULD win - recall
    routed_to: dict[str, int] = defaultdict(int)  # by the skill the Lead LOADED - precision denom
    correct_for: dict[str, int] = defaultdict(int)
    for c in cases:
        per_expected[c.expect].append(c)
        routed_to[c.chosen] += 1
        if c.ok:
            correct_for[c.expect] += 1

    print("  per-skill  (recall = found / should · precision = right / routed-here)")
    for skill in sorted(per_expected):
        should = per_expected[skill]
        recall = correct_for[skill] / len(should)
        routed = routed_to.get(skill, 0)
        precision = (correct_for[skill] / routed) if routed else 1.0
        flag = "" if recall == 1.0 and precision == 1.0 else "  ←"
        print(f"    {skill:<10} recall {recall:5.0%}  precision {precision:5.0%}{flag}")

    misses = [c for c in cases if not c.ok]
    if misses:
        print("\n  misroutes:")
        for c in misses:
            print(f"    ✗ expect {c.expect:<10} got {c.chosen or 'none':<10} · {c.goal[:56]}")

    accuracy = sum(1 for c in cases if c.ok) / len(cases) if cases else 0.0
    return accuracy


async def run(concurrency: int = 6, threshold: float = 0.95) -> int:
    cases, index = await _load_cases_and_index()
    if not cases:
        print("no builtin routing cases - run `just migrate` (seeds the twelve)")
        return 1

    print(
        f"── routing: {len(cases)} cases across {len(index.described)} skills, {concurrency} at a time ──\n"
    )
    await _route_all(cases, index, concurrency)
    accuracy = _report(cases)

    print(f"\n── {sum(1 for c in cases if c.ok)}/{len(cases)} routed correctly  ({accuracy:.0%}) ──")
    if accuracy < threshold:
        print(f"\nFAIL: {accuracy:.0%} is below the {threshold:.0%} gate")
        return 1
    print(f"\nPASS: {accuracy:.0%} ≥ {threshold:.0%}")
    return 0
