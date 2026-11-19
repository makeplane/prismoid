# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Which dataset rows a run asks: `--rows`, `--tag`, `--match`, `--failed-in` and `--sample`, joined by AND.

The flags filter before anything is submitted, and the report keeps the `Selection` with the ids it
resolved to, so a partial run is never read as the whole suite.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from evals.schema import OutcomeRow

NOT_SELECTED = "not selected"
OUTSIDE_SAMPLE = "outside --sample"


def tags_of(row: OutcomeRow) -> frozenset[str]:
    """A row's tags: its group, category, variant and expected outcome."""
    found = (row.group, row.category, row.variant, row.expected_outcome)
    return frozenset(str(tag) for tag in found if tag)


class Sample(BaseModel):
    """`--sample N --seed S`: N runnable rows drawn by a seeded RNG, kept in dataset order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: int
    seed: int = 0

    def draw(self, runnable: Sequence[OutcomeRow]) -> tuple[list[OutcomeRow], list[str]]:
        """The drawn rows, and the ids of the runnable rows left out."""
        picked = set(random.Random(self.seed).sample(range(len(runnable)), min(self.size, len(runnable))))
        kept = [r for n, r in enumerate(runnable) if n in picked]
        return kept, [r.id for n, r in enumerate(runnable) if n not in picked]


class Selection(BaseModel):
    """The selection flags as given; `failed` and `chosen` are the ids they resolved to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    match: str = ""
    failed_in: str = ""  # the earlier report's path
    failed: tuple[str, ...] = ()  # the rows that failed in it
    sample: Sample | None = None
    chosen: tuple[str, ...] = ()  # the rows this run asked

    @property
    def partial(self) -> bool:
        return bool(self.rows or self.tags or self.match or self.failed_in or self.sample)

    def pick(self, rows: Sequence[OutcomeRow]) -> tuple[list[OutcomeRow], dict[str, str]]:
        """The rows every filter keeps, and the rest as not selected; ValueError names an unknown id or tag."""
        known = {r.id for r in rows}
        if unknown := [rid for rid in self.rows if rid not in known]:
            raise ValueError(f"unknown row id: {', '.join(unknown)}")
        carried = frozenset[str]().union(*(tags_of(r) for r in rows))
        if unknown := [tag for tag in self.tags if tag not in carried]:
            raise ValueError(f"no row carries tag: {', '.join(unknown)}")
        chosen = [r for r in rows if self._keeps(r)]
        if self.partial and not chosen:
            raise ValueError("the selection selects no row")
        kept = {r.id for r in chosen}
        return chosen, {r.id: NOT_SELECTED for r in rows if r.id not in kept}

    def _keeps(self, row: OutcomeRow) -> bool:
        needle = self.match.casefold()
        haystack = (row.id, row.message, row.label_note or "")
        return (
            (not self.rows or row.id in self.rows)
            and (not self.tags or not tags_of(row).isdisjoint(self.tags))
            and (not needle or any(needle in text.casefold() for text in haystack))
            and (not self.failed_in or row.id in self.failed)
        )
