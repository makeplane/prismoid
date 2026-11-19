# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a turn's summary is made of: each step's narration, keyed by the step,
and a sink fence 2 re-drafted takes its words out - the re-draft's replace them."""

from prismoid.jobs.run_task import kept_narrations, summary_of


def test_the_summary_joins_what_the_steps_said() -> None:
    assert summary_of([("a", "Found it."), ("b", "  "), ("c", "Wrote it.")]) == "Found it.\n\nWrote it."


def test_a_redrafted_sinks_narration_leaves_the_summary() -> None:
    said = [("research", "Found it."), ("memo", "Wrote it in dollars.")]
    kept = kept_narrations(said, ["memo"])
    kept.append(("memo-2", "Wrote it in euros."))
    assert summary_of(kept) == "Found it.\n\nWrote it in euros."


def test_nothing_replaced_keeps_every_narration() -> None:
    said = [("a", "one"), ("b", "two")]
    assert kept_narrations(said, []) == said
