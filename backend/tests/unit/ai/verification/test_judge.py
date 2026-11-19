# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The judge - the two invariants that make a green tick worth reading.

Neither needs a model, and that is the point: they are **structural**. A rule enforced by asking a
model nicely is a rule that will be broken by a model that did not read the ask.

The judge never overturns a deterministic FAIL, and a PASS needs something that examined the content:
the suite's grounded checks, or the judge's criteria read over work it was shown. Only when nobody
examined anything does the UNVERIFIED mark stay, which is what makes that mark mean something.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, cast

import pytest

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.resolve import chain_for, resolve
from prismoid.ai.verification.pipeline import final_verdict
from prismoid.errors import PolicyDenied
from prismoid.schemas.model_class import Family, JudgeFamily, ModelClass
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import (
    Check,
    CheckKind,
    CheckStatus,
    JudgeOpinion,
    Verdict,
    VerificationReport,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def spec(provider: str, model: str, family: str, *classes: ModelClass) -> ModelSpec:
    return ModelSpec(
        provider=provider,
        model_id=model,
        family=Family(family),
        protocol="anthropic",
        classes=tuple(classes),
    )


CATALOG = [
    spec("anthropic", "big", "anthropic", ModelClass.JUDGE, ModelClass.FAST_EXECUTOR),
    spec("anthropic", "small", "anthropic", ModelClass.FAST_EXECUTOR),
    spec("openai", "big", "openai", ModelClass.JUDGE),
    spec("local", "qwen", "qwen", ModelClass.JUDGE),
]


# ── invariant 1: the receipt names how far the judge sat from the maker ───────────────────────


def test_an_excluded_family_never_reaches_the_judge_chain() -> None:
    """`exclude_family` is a catalog filter, not a request in a prompt: the excluded family is gone
    before a model is constructed (the answer evals judge across families with it)."""
    for producer in ("anthropic", "openai", "qwen"):
        chain = chain_for(ModelClass.JUDGE, CATALOG, exclude_family=Family(producer))
        assert chain and all(m.family != producer for m in chain)


def test_resolve_raises_when_every_judge_is_excluded() -> None:
    """A filtered-out chain is an error, never the excluded family handed back in silence."""
    only_ours = [spec("anthropic", "big", "anthropic", ModelClass.JUDGE)]
    with pytest.raises(PolicyDenied, match="judge"):
        resolve(ModelClass.JUDGE, only_ours, exclude_family=Family("anthropic"))


@pytest.mark.parametrize(
    ("judge", "seat"),
    [
        (
            JudgeOpinion(verdict=Verdict.PASS, rationale="…", model="openai:big", family=Family("openai")),
            JudgeFamily.OTHER,
        ),
        (
            JudgeOpinion(
                verdict=Verdict.PASS, rationale="…", model="anthropic:small", family=Family("anthropic")
            ),
            JudgeFamily.SAME,
        ),
        (
            JudgeOpinion(
                verdict=Verdict.PASS, rationale="…", model="anthropic:big", family=Family("anthropic")
            ),
            JudgeFamily.SELF,
        ),
        (None, JudgeFamily.NONE),
    ],
)
def test_the_signed_report_names_the_judges_seat(judge: JudgeOpinion | None, seat: JudgeFamily) -> None:
    """Read from the signed models, so the label cannot drift from what actually judged."""
    report = VerificationReport(
        artifact_sha256="ab" * 32,
        suite=VerifierSuite.DOCUMENT,
        judge=judge,
        verdict=Verdict.PASS,
        producer_model="anthropic:big",
        producer_family=Family("anthropic"),
        at=dt.datetime(2026, 11, 14, tzinfo=dt.UTC),
    )
    assert report.judged is seat


# ── invariant 2: a deterministic FAIL is a floor; the rubric follows the work ──────────────────


def opinion(verdict: Verdict) -> JudgeOpinion:
    return JudgeOpinion(verdict=verdict, rationale="…", model="x:y", family=Family("openai"))


def checks(*statuses: CheckStatus) -> list[Check]:
    return [Check(name=f"c{i}", status=s, kind=CheckKind.SUBSTANTIVE) for i, s in enumerate(statuses)]


@pytest.mark.parametrize(
    ("suite", "judge", "read", "expected"),
    [
        # The suite failed. Nothing the judge says can rescue it - the floor holds.
        ([CheckStatus.FAIL], Verdict.PASS, True, Verdict.FAIL),
        ([CheckStatus.FAIL], Verdict.PASS, False, Verdict.FAIL),
        ([CheckStatus.FAIL], None, True, Verdict.FAIL),
        # The suite passed, and the judge - reading what the suite could not - fails it.
        ([CheckStatus.PASS], Verdict.FAIL, True, Verdict.FAIL),
        ([CheckStatus.PASS], Verdict.PASS, True, Verdict.PASS),
        # The suite had NOTHING to examine (an unsourced original document skips every
        # grounding check). The judge's read against the acceptance criteria IS the
        # substantive examination - its verdict decides, either way (the old
        # frozen-UNVERIFIED law meant 160 unsourced artifacts and zero passes, ever).
        ([CheckStatus.SKIP], Verdict.PASS, True, Verdict.PASS),
        ([CheckStatus.SKIP], Verdict.FAIL, True, Verdict.FAIL),
        # ...but only when the judge was shown the content: a PASS over a placeholder signs nothing.
        ([CheckStatus.SKIP], Verdict.PASS, False, Verdict.UNVERIFIED),
        # Nobody examined the content at all: the honest mark stays.
        ([CheckStatus.SKIP], None, True, Verdict.UNVERIFIED),
        # No judge at all: the deterministic verdict stands for exactly what it proved.
        ([CheckStatus.PASS], None, True, Verdict.PASS),
        # The suite examined the content itself, so what the judge saw does not matter.
        ([CheckStatus.PASS], Verdict.PASS, False, Verdict.PASS),
    ],
)
def test_the_rubric_follows_the_work_and_a_fail_is_a_floor(
    suite: list[CheckStatus], judge: Verdict | None, read: bool, expected: Verdict
) -> None:
    """A FAIL cannot be talked out of, and a PASS must come from whoever actually looked."""
    verdict = final_verdict(checks(*suite), opinion(judge) if judge else None, judge_read_content=read)
    assert verdict is expected


# ── the producer's family is the catalog's, not the provider's (found live 2026-10-25) ────────


async def test_the_producers_family_comes_from_its_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseten serves Zhipu's GLM. Keyed on the provider, GLM judging its own work read as another
    family; keyed on the catalog row, the receipt says self-checked."""
    from prismoid.operations import models as model_ops
    from prismoid.operations import verification

    glm = ModelSpec(
        provider="baseten",
        model_id="zai-org/GLM-5.3-Flash",
        family=Family("zhipu"),
        protocol="openai_compatible",
        classes=(ModelClass.FAST_EXECUTOR, ModelClass.JUDGE),
        base_url="https://inference.baseten.co/v1",
    )

    async def catalog(_session: object, *, enabled_only: bool = True) -> list[ModelSpec]:
        assert enabled_only is False, "a model disabled since it wrote the work is still its author"
        return [glm]

    monkeypatch.setattr(model_ops, "catalog", catalog)
    session = cast("AsyncSession", object())
    assert await verification.author_family(session, "baseten:zai-org/GLM-5.3-Flash") == Family("zhipu")
    assert await verification.author_family(session, "anthropic:claude-gone-1") == Family("anthropic")
    maker = await verification.author_family(session, glm.ref)
    assert (
        JudgeFamily.between(maker=glm.ref, maker_family=maker, judge=glm.ref, judge_family=glm.family)
        is JudgeFamily.SELF
    )


# ── the one candidate loop: a seat that fails falls through; pages reach only a seat that sees ──


async def test_a_lost_seat_falls_through_and_only_a_sighted_seat_sees_the_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from decimal import Decimal
    from types import SimpleNamespace

    from pydantic_ai.messages import BinaryContent

    from prismoid.ai.agents import base as agents
    from prismoid.ai.deps import TaskDeps
    from prismoid.ai.gateway.resolve import JudgeSeats
    from prismoid.ai.verification.pipeline import PageImage
    from prismoid.operations import verification
    from prismoid.schemas.ids import TaskId
    from prismoid.schemas.usage import UsageRecord
    from prismoid.schemas.verification import JudgeVerdict

    lost = spec("openai", "lost", "openai", ModelClass.JUDGE, ModelClass.VISION)
    blind = spec("qwen", "blind", "qwen", ModelClass.JUDGE)
    sighted = spec("google", "sighted", "google", ModelClass.JUDGE, ModelClass.VISION)
    asked: dict[str, object] = {}

    def build(name: str, deps: TaskDeps, catalog: list[ModelSpec], **_: object) -> object:
        (seat,) = catalog

        async def run(prompt: object) -> object:
            asked[seat.model_id] = prompt
            if seat is lost:
                raise TimeoutError("no answer")
            assert deps.emit_usage is not None
            record = UsageRecord(
                provider=seat.provider,
                model=seat.model_id,
                family=seat.family,
                units={},
                cost_usd=Decimal(0),
                price_version=1,
                at=dt.datetime.now(dt.UTC),
            )
            await deps.emit_usage(record)
            return SimpleNamespace(output=JudgeVerdict(verdict=Verdict.PASS, rationale="read it"))

        assert name == "judge"
        return SimpleNamespace(run=run)

    monkeypatch.setattr(agents, "build", build)
    pages = (PageImage(1, b"\x89PNG1"), PageImage(4, b"\x89PNG4"))
    deps = TaskDeps(task_id=TaskId(uuid.uuid4()))

    judged = await verification.ask_judge(
        JudgeSeats(JudgeFamily.OTHER, (lost, sighted)), deps, "the work", pages, seconds=5
    )
    assert judged is not None and judged.model == sighted.ref and judged.family == Family("google")
    seen = asked["sighted"]
    assert isinstance(seen, list) and seen[0] == "the work" and "[page 4]" in seen
    assert [part.data for part in seen if isinstance(part, BinaryContent)] == [b"\x89PNG1", b"\x89PNG4"]

    await verification.ask_judge(JudgeSeats(JudgeFamily.OTHER, (blind,)), deps, "the work", pages, seconds=5)
    assert isinstance(asked["blind"], str) and "could not be shown" in str(asked["blind"])
    assert await verification.ask_judge(JudgeSeats(JudgeFamily.NONE, ()), deps, "w", pages, seconds=5) is None


async def test_each_judge_seat_reads_the_web_inside_its_own_wall(monkeypatch: pytest.MonkeyPatch) -> None:
    """A judge's cited-page reads see the seat's wall, so a slow page never outlasts it; the next seat gets a fresh one."""
    import time
    import uuid
    from types import SimpleNamespace

    from prismoid.ai.agents import base as agents
    from prismoid.ai.deps import TaskDeps
    from prismoid.ai.gateway.resolve import JudgeSeats
    from prismoid.operations import verification
    from prismoid.schemas.ids import TaskId

    first = spec("openai", "first", "openai", ModelClass.JUDGE)
    second = spec("google", "second", "google", ModelClass.JUDGE)
    walls: list[float] = []

    def build(_name: str, deps: TaskDeps, _catalog: list[ModelSpec], **_: object) -> object:
        async def run(_prompt: object) -> object:
            assert deps.deadline is not None
            walls.append(deps.deadline - time.monotonic())
            raise TimeoutError("no answer")

        return SimpleNamespace(run=run)

    monkeypatch.setattr(agents, "build", build)
    deps = TaskDeps(task_id=TaskId(uuid.uuid4()))
    seats = JudgeSeats(JudgeFamily.OTHER, (first, second))
    assert await verification.ask_judge(seats, deps, "the work", (), seconds=30) is None
    assert len(walls) == 2 and all(25 < wall <= 30 for wall in walls)
    assert deps.deadline is None, "the caller's deps keep their own wall"
