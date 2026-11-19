# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The decider class and the judge's seat, as catalog data: the decider resolves to a decision model
from whichever key the host holds, and the judge sits as far from the maker as the keys allow:
another family, else another model of its family, else the maker itself, else no judge."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import yaml

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.model_class import Family, JudgeFamily, ModelClass
from prismoid.schemas.usage import Unit
from tests.support.paths import BACKEND

if TYPE_CHECKING:
    from prismoid.ai.gateway.resolve import JudgeSeats

STARTER = yaml.safe_load((BACKEND / "prismoid" / "runner" / "default_models.yaml").read_text())


def _starter_rows() -> list[ModelSpec]:
    """The starter catalog's enabled chat rows, as the values `operations` hands the gateway."""
    return [
        ModelSpec(
            provider=r["provider"],
            model_id=r["model_id"],
            family=Family(r["family"]),
            protocol=r["protocol"],
            classes=tuple(ModelClass(c) for c in r["classes"]),
            prices={Unit(u): Decimal(str(p)) for u, p in r["prices"].items()},
        )
        for r in STARTER["models"]
        if r["enabled"] and r["protocol"] in ("anthropic", "openai", "google")
    ]


def _row(provider: str, model_id: str, *classes: ModelClass) -> ModelSpec:
    return ModelSpec(
        provider=provider, model_id=model_id, family=Family(provider), protocol=provider, classes=classes
    )


def _keys(monkeypatch: pytest.MonkeyPatch, *providers: str) -> None:
    """Hermetic: only the named providers hold a key, never the developer's `.env`."""
    from prismoid.ai.gateway import providers as gateway_providers

    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        gateway_providers, "api_key_for", lambda provider: "k" if provider in providers else ""
    )


def _served_by(model: object) -> str:
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    from prismoid.ai.gateway.meter import MeteredModel

    assert isinstance(model, ConcurrencyLimitedModel) and isinstance(model.wrapped, MeteredModel)
    return model.wrapped.spec.ref


def test_with_only_an_anthropic_key_the_decider_is_the_haiku_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.gateway.resolve import resolve

    _keys(monkeypatch, "anthropic")
    model = resolve(ModelClass.DECIDER, _starter_rows(), order=STARTER["routing"])
    assert _served_by(model) == "anthropic:claude-haiku-5-5"


def test_the_decider_chain_is_luna_then_haiku_then_gemini_flash() -> None:
    from prismoid.ai.gateway.resolve import chain_for

    chain = chain_for(ModelClass.DECIDER, _starter_rows(), order=STARTER["routing"]["decider"])
    assert [m.ref for m in chain] == [
        "openai:gpt-6-luna",
        "anthropic:claude-haiku-5-5",
        "google:gemini-3.8-flash",
    ]


def test_the_decider_leads_with_luna_and_ends_on_a_google_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.gateway.resolve import resolve

    _keys(monkeypatch, "openai")
    assert (
        _served_by(resolve(ModelClass.DECIDER, _starter_rows(), order=STARTER["routing"]))
        == "openai:gpt-6-luna"
    )
    _keys(monkeypatch, "google")
    assert _served_by(resolve(ModelClass.DECIDER, _starter_rows(), order=STARTER["routing"])) == (
        "google:gemini-3.8-flash"
    )


LUNA = _row("openai", "gpt-6-luna", ModelClass.JUDGE, ModelClass.DECIDER)
SOL = _row("openai", "gpt-6-sol", ModelClass.JUDGE)
HAIKU = _row("anthropic", "claude-haiku-5-5", ModelClass.JUDGE, ModelClass.DECIDER)
ORDER = {ModelClass.JUDGE.value: [LUNA.ref, SOL.ref, HAIKU.ref]}


def _seats_for_luna(catalog: list[ModelSpec]) -> JudgeSeats:
    """The judge seats for work `gpt-6-luna` made, under the routing order above."""
    from prismoid.ai.gateway.resolve import judge_seats

    return judge_seats(catalog, maker_family=LUNA.family, maker_model=LUNA.ref, order=ORDER)


def test_with_both_keys_the_judge_of_a_luna_made_file_is_another_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prismoid.ai.gateway.resolve import JudgeSeats

    _keys(monkeypatch, "openai", "anthropic")
    assert _seats_for_luna([LUNA, SOL, HAIKU]) == JudgeSeats(JudgeFamily.OTHER, (HAIKU,))


def test_with_one_key_another_model_of_the_family_judges_before_the_maker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prismoid.ai.gateway.resolve import JudgeSeats

    _keys(monkeypatch, "openai")
    assert _seats_for_luna([LUNA, SOL, HAIKU]) == JudgeSeats(JudgeFamily.SAME, (SOL,))


def test_with_one_key_and_one_judge_row_the_maker_checks_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.gateway.resolve import JudgeSeats

    _keys(monkeypatch, "openai")
    seats = _seats_for_luna([LUNA, HAIKU])
    assert seats == JudgeSeats(JudgeFamily.SELF, (LUNA,))
    assert f"judged: {JudgeFamily.SELF}" == "judged: self-checked"


def test_no_judge_row_with_a_key_is_no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.gateway.resolve import JudgeSeats

    _keys(monkeypatch)
    assert _seats_for_luna([LUNA, SOL, HAIKU]) == JudgeSeats(JudgeFamily.NONE, ())
