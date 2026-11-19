# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scatter shape's laws (ADR-050) - pure, and SIMULATED.

Two halves. The direct laws: the map step, the validator's legible refusals, the expansion
shape, canonicalization, the tally's four verdicts. Then **the simulator** - the owner's
requirement made a test: seeded multi-round narratives drive `tally`/`rescatter` through
scripted seat outcomes (undercounts, duplicates, aliases, dead seats, exhausted ground)
and assert the loop's INVARIANTS on every path: coverage never decreases, rounds never
exceed the cap, exclusions only grow, exhausted ground is never re-plowed, and every
narrative ends in exactly one of the tally's honest reasons. No model, no I/O, milliseconds.
"""

from __future__ import annotations

import random

import pytest

from prismoid.operations.swarm import (
    InvalidScatter,
    canonicalize,
    dedupe,
    expand,
    records_filename,
    rescatter,
    scatter_plan,
    tally,
    validate_scatter,
)
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.swarm import SEAT_GOAL_MAX, ResearchRecord, ScatterResult, SeatBrief


def _seat(i: int) -> SeatBrief:
    return SeatBrief(
        id=f"g{i:02d}",
        goal=f"Sweep territory {i}: registries and directories for region {i}",
        not_here=f"regions other than {i} - siblings own those",
        expected=f"one qualifying entity from region {i} with its source",
    )


def _scatter(seats: int = 3, target: int = 12) -> ScatterResult:
    return ScatterResult(
        objective="find qualifying entities",
        entity="a qualifying entity",
        target_count=target,
        record_fields=["name", "year"],
        seats=[_seat(i + 1) for i in range(seats)],
    )


def _record(name: str, url: str = "https://example.com/x") -> ResearchRecord:
    return ResearchRecord(entity=name, url=url, excerpts=["verbatim proof"], answer={"name": name})


# ── the map ───────────────────────────────────────────────────────────────────────────────


def test_the_plan_is_one_researcher_map_step() -> None:
    steps, edges, _ = scatter_plan("Find at least 12 things")
    assert len(steps) == 1 and not edges
    spec = steps[0][1]
    assert str(spec.agent) == "researcher" and spec.lane_role == "scatter_map"
    assert spec.verify is False and spec.artifact_role == "working"
    assert spec.contract is not None and spec.contract.goal == "Find at least 12 things"


def test_a_recipe_count_rides_the_maps_acceptance() -> None:
    (_, spec), *_ = scatter_plan("Find climate startups", target_count=60)[0]
    assert spec.contract is not None
    assert any("target_count is 60" in line for line in spec.contract.acceptance)
    (_, bare), *_ = scatter_plan("Find climate startups")[0]
    assert bare.contract is not None and not any("target_count" in line for line in bare.contract.acceptance)


# ── the validator's legible refusals ──────────────────────────────────────────────────────


def test_validator_refusals_name_their_fix() -> None:
    # The schema already refuses 1 seat at construction - smuggle the shapes past pydantic
    # with model_copy so the VALIDATOR's own refusal (the retry gotcha) is what's tested.
    lone = _scatter(2).model_copy(update={"seats": [_seat(1)]})
    with pytest.raises(InvalidScatter, match="at least 2"):
        validate_scatter(lone, max_width=8)
    wide = _scatter(seats=9)
    with pytest.raises(InvalidScatter, match="merge adjacent territories"):
        validate_scatter(wide, max_width=8)
    dup = _scatter(3)
    clone = dup.model_copy(update={"seats": [dup.seats[0], dup.seats[0], dup.seats[2]]})
    with pytest.raises(InvalidScatter, match="share an id"):
        validate_scatter(clone, max_width=8)


def test_expand_builds_the_wave_and_the_quorum_merge() -> None:
    scatter = _scatter(3)
    steps, edges = expand(scatter, goal="the ask", map_step=scatter_plan("x")[0][0][0], max_width=8)
    specs = [s for _, s in steps]
    gathers = [s for s in specs if s.lane_role == "gather"]
    synthesis = [s for s in specs if s.lane_role == "synthesis"]
    assert len(gathers) == 3 and len(synthesis) == 1
    assert all(s.scatter_round == 1 and s.verify is False for s in gathers)
    assert synthesis[0].join == "quorum" and synthesis[0].tolerates_missing_inputs
    # every gather edges into synthesis; the map edges into both
    to_synth = sum(1 for e in edges if str(e.to_step) == str(steps[-1][0]))
    assert to_synth == 4  # 3 seats + the map (the bible reaches the merge)


# ── identity ──────────────────────────────────────────────────────────────────────────────


def test_canonicalize_folds_aliases_accents_and_keeps_non_latin() -> None:
    assert canonicalize("Acme, Inc.") == canonicalize("ACME Inc") == "acme"
    assert canonicalize("Señor Widgets GmbH") == "senor widgets"  # accents FOLD
    assert canonicalize("Müller GmbH") != canonicalize("Möller AG")  # …and stay distinct
    assert canonicalize("深圳腾讯") == "深圳腾讯"  # non-Latin survives - '' dropped records
    assert canonicalize("Яндекс") == "яндекс"
    assert canonicalize("Alpha Corp") != canonicalize("Beta Corp")


def test_dedupe_never_silently_drops_a_record() -> None:
    """The review's exact failure: an ASCII-only fold erased non-Latin names to '' and the
    tally counted 0 unique over a full harvest (with '' in the exclusion list)."""
    records = [_record("深圳腾讯"), _record("Яндекс"), _record("深圳腾讯"), _record("***")]
    assert [r.entity for r in dedupe(records)] == ["深圳腾讯", "Яндекс", "***"]


def test_dedupe_is_first_wins_per_canonical_entity() -> None:
    records = [_record("Acme, Inc."), _record("ACME Inc"), _record("Beta LLC")]
    unique = dedupe(records)
    assert [r.entity for r in unique] == ["Acme, Inc.", "Beta LLC"]


# ── the tally's four honest verdicts ──────────────────────────────────────────────────────


def test_tally_verdicts() -> None:
    scatter = _scatter(target=4)
    full = [_record(f"e{i}") for i in range(4)]
    assert tally(scatter, full, rounds_used=1, max_rounds=2, all_exhausted=False).reason == "target-met"
    short = full[:2]
    assert tally(scatter, short, rounds_used=1, max_rounds=2, all_exhausted=False).reason == "continue"
    assert (
        tally(scatter, short, rounds_used=2, max_rounds=2, all_exhausted=False).reason == "rounds-exhausted"
    )
    assert tally(scatter, short, rounds_used=1, max_rounds=2, all_exhausted=True).reason == "ground-exhausted"


def test_rescatter_never_replows_exhausted_ground_and_shrinks_with_the_gap() -> None:
    scatter = _scatter(seats=4, target=40)
    wave = rescatter(
        scatter, round_no=2, gap=6, exclusions=["a", "b"], exhausted_fences=["g01", "g02"], max_width=8
    )
    ids = [spec.scatter_seat for _, spec in wave]
    assert ids and all(i in ("g03", "g04") for i in ids), "exhausted fences never re-plowed"
    assert len(wave) <= 2, "a 6-entity gap never redeploys the full width"
    for _, spec in wave:
        assert spec.scatter_round == 2
        assert "never re-submit these entities" in (spec.contract.goal if spec.contract else "")


def test_rescatter_rider_fits_the_goal_cap() -> None:
    """Review F8: a near-cap round-1 goal must survive re-briefing - SeatBrief re-validates
    max_length at rescatter, and an overflow once silently cost the whole gap round."""
    long_seat = SeatBrief(
        id="g01",
        goal="Sweep the registries: " + "x" * (SEAT_GOAL_MAX - 30),
        not_here="the siblings' ground stays theirs",
        expected="one qualifying finding",
    )
    scatter = _scatter(seats=2, target=60).model_copy(update={"seats": [long_seat, _seat(2)]})
    wave = rescatter(scatter, round_no=2, gap=16, exclusions=[], exhausted_fences=["g02"], max_width=8)
    assert [spec.scatter_seat for _, spec in wave] == ["g01"]
    goal = wave[0][1].contract.goal if wave[0][1].contract else ""
    assert "hunt the misses" in goal, "the rider survived the truncation"


def test_records_filename_is_round_suffixed_after_round_one() -> None:
    """Review F5: a re-scattered fence must never republish its round-1 path - the
    synthesis mount is last-write-wins per path."""
    assert records_filename("g01", 1) == "records-g01.jsonl"
    assert records_filename("g01", 2) == "records-g01-r2.jsonl"
    assert records_filename("vega-altair-declarative", 3) == "records-vega-altair-declarative-r3.jsonl"


def test_rescatter_keeps_the_fence_id_verbatim() -> None:
    """Run 5's live catch, pinned: a 23-char hyphenated map-written id must survive a
    re-scatter - no suffix (the old `{id}r{n}` overflowed the cap), no title parsing."""
    long_seat = SeatBrief(
        id="vega-altair-declarative",  # 23 chars, hyphens, contains 'r'
        goal="Sweep the Vega/Altair declarative branch of the landscape",
        not_here="imperative plotting - a sibling owns it",
        expected="one qualifying package with repo and license",
    )
    scatter = _scatter(seats=2, target=40).model_copy(update={"seats": [_seat(1), long_seat]})
    wave = rescatter(scatter, round_no=2, gap=20, exclusions=[], exhausted_fences=["g01"], max_width=8)
    assert [spec.scatter_seat for _, spec in wave] == ["vega-altair-declarative"]
    assert all("round 2" in spec.title for _, spec in wave)


# ── THE SIMULATOR (the owner's requirement) ───────────────────────────────────────────────


def _simulate(seed: int) -> str:
    """One narrative: a scatter with random target/width; each round, every live seat yields
    a scripted haul (some duplicates, some aliases, sometimes death, sometimes exhaustion);
    the loop runs tally → rescatter exactly as operations does. Returns the final reason.
    Asserts the invariants at every step."""
    rng = random.Random(seed)
    seats = rng.randint(2, 6)
    target = rng.randint(8, 60)
    scatter = _scatter(seats=seats, target=target)
    max_rounds = rng.choice([1, 2, 3])

    found: list[ResearchRecord] = []
    exhausted: set[str] = set()
    live_briefs = list(scatter.seats)
    rounds_used = 0
    prev_unique = 0
    prev_exclusions: set[str] = set()

    while True:
        rounds_used += 1
        for seat in live_briefs:
            if rng.random() < 0.15:
                continue  # the seat DIED (skipped) - costs the seat, never the sweep
            haul = rng.randint(0, max(1, target // max(1, seats)))
            for _ in range(haul):
                name = f"entity-{rng.randint(1, target * 2)}"
                if rng.random() < 0.2:
                    name = name.upper() + ", Inc."  # an alias of a maybe-known entity
                found.append(_record(name))
            if rng.random() < 0.3:
                exhausted.add(seat.id)  # fence ids verbatim, every round

        all_exhausted = {s.id for s in scatter.seats} <= exhausted
        verdict = tally(
            scatter, found, rounds_used=rounds_used, max_rounds=max_rounds, all_exhausted=all_exhausted
        )

        # invariants, every round
        assert verdict.unique >= prev_unique, "coverage never decreases"
        assert rounds_used <= max_rounds + 1, "the round cap holds"
        prev_unique = verdict.unique

        if verdict.done:
            assert verdict.reason in ("target-met", "rounds-exhausted", "ground-exhausted")
            if verdict.reason == "target-met":
                assert verdict.unique >= target
            return verdict.reason

        exclusions = {canonicalize(r.entity) for r in found}
        assert exclusions >= prev_exclusions, "exclusions only grow"
        prev_exclusions = exclusions
        wave = rescatter(
            scatter,
            round_no=rounds_used + 1,
            gap=verdict.gap,
            exclusions=sorted(exclusions),
            exhausted_fences=sorted(exhausted),
            max_width=8,
        )
        assert all(spec.scatter_seat not in exhausted for _, spec in wave), "exhausted ground never re-plowed"
        if not wave:
            # every fence exhausted mid-narrative: the next tally must close it honestly
            verdict = tally(
                scatter, found, rounds_used=rounds_used, max_rounds=max_rounds, all_exhausted=True
            )
            assert verdict.done
            return verdict.reason
        live_briefs = [
            SeatBrief(
                id=spec.scatter_seat or "seat",
                goal="re-sweep the assigned territory",
                not_here="the siblings' ground stays theirs",
                expected="one qualifying finding",
            )
            for _, spec in wave
        ]


@pytest.mark.parametrize("seed", range(40))
def test_simulated_narratives_hold_every_invariant(seed: int) -> None:
    reason = _simulate(seed)
    assert reason in ("target-met", "rounds-exhausted", "ground-exhausted")


def test_gather_spec_round_and_seat_stamps_survive_the_wire() -> None:
    scatter = _scatter(2)
    steps, _ = expand(scatter, goal="x", map_step=scatter_plan("x")[0][0][0], max_width=8)
    for _, spec in steps:
        if spec.lane_role == "gather":
            wired = StepSpec.model_validate(spec.model_dump(mode="json"))
            assert wired.scatter_round == 1
            assert wired.scatter_seat in ("g01", "g02")


# -- the synthesis digest (the scatter finale's thread answer) ------------------------


def test_synthesis_digest_returns_a_short_document_verbatim() -> None:
    from prismoid.schemas.swarm import synthesis_digest

    doc = "# Ten databases\n\n| name | license |\n| --- | --- |\n| Qdrant | Apache-2.0 |"
    assert synthesis_digest(doc) == doc


def test_synthesis_digest_cuts_at_block_boundaries_and_names_the_cut() -> None:
    from prismoid.schemas.swarm import synthesis_digest

    blocks = [f"Paragraph {i} " + "x" * 300 for i in range(10)]
    out = synthesis_digest("\n\n".join(blocks), cap=1000)
    # whole blocks only - a kept paragraph is intact, and the cut says where the rest lives
    assert out.startswith("Paragraph 0")
    assert "Paragraph 3" not in out
    assert out.endswith("*The full document continues in the artifact.*")


def test_synthesis_digest_never_tears_a_table_row() -> None:
    from prismoid.schemas.swarm import synthesis_digest

    table = "| a | b |\n" * 40  # one ~400-char block
    doc = "Intro.\n\n" + table + "\n\nOutro."
    out = synthesis_digest(doc, cap=200)
    # the table exceeds the room: torn at a ROW boundary now - head rows
    # survive under the cap, no row is ever split, and the cut still names itself
    assert out.startswith("Intro.")
    body = out.removesuffix("\n\n*The full document continues in the artifact.*")
    assert body != out and len(body) <= 200
    rows = body.splitlines()[2:]
    assert rows and all(line == "| a | b |" for line in rows)
    assert "Outro." not in out


def test_synthesis_digest_hard_cuts_a_single_oversized_block() -> None:
    # The review's first defect: `if kept and ...` exempted block one, so a single-block
    # 23KB findings table came back WHOLE and the cap never bound.
    from prismoid.schemas.swarm import synthesis_digest

    table = "".join(f"| row {i:04d} | value |\n" for i in range(1000)).rstrip()
    out = synthesis_digest(table, cap=600)
    body = out.removesuffix("\n\n*The full document continues in the artifact.*")
    assert body != out and len(body) <= 600  # capped, and the cut named
    assert body.startswith("| row 0000 | value |")
    assert all(line.endswith("| value |") for line in body.splitlines())  # line-boundary tears only


def test_synthesis_digest_keeps_the_head_rows_of_an_oversized_findings_table() -> None:
    # The review's second defect: '# Heading' + big table kept the heading and DROPPED the
    # findings - the table must survive truncated, never vanish.
    from prismoid.schemas.swarm import synthesis_digest

    table = "".join(f"| db {i:03d} | Apache-2.0 |\n" for i in range(500)).rstrip()
    out = synthesis_digest("# Ten thousand databases\n\n" + table, cap=600)
    assert out.startswith("# Ten thousand databases")
    assert "| db 000 | Apache-2.0 |" in out
    body = out.removesuffix("\n\n*The full document continues in the artifact.*")
    assert body != out and len(body) <= 600
