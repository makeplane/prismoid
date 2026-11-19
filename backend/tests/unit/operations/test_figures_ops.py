# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Figure pre-generation (ADR-030), asserted - above all the MONEY pins. The CRITICAL was one
missing call: the job bound `units_emitter` per candidate and
never invoked it, so every pre-generated image was an unmetered provider call. The fan-out
core (`generate_batch`) is pure of persistence exactly so these pins can hold Rule 8 on it
with fake wires and nothing stood up."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from prismoid.ai.tools.providers.images import ImageCandidate
from prismoid.db.models.task import Task
from prismoid.operations import figures
from prismoid.schemas.plan import TaskStatus

Ledger = list[tuple[str, dict[str, int]]]


@contextlib.asynccontextmanager
async def _no_throttle(_provider: str) -> AsyncGenerator[None]:
    yield


@pytest.fixture(autouse=True)
def _quiet_gateway(  # pyright: ignore[reportUnusedFunction] - autouse: pytest calls it, not us
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(figures, "throttled", _no_throttle)


def _candidate(name: str, ledger: Ledger) -> ImageCandidate:
    async def emit(units: dict[str, int]) -> None:
        ledger.append((name, units))

    return ImageCandidate(
        provider=name,
        model_id=f"{name}-img",
        protocol="openai_images",
        base_url=None,
        api_key="k",
        emit_units=emit,
    )


# ── Rule 8: one emission per landed image, none for anything else ─────────────────────────────


def test_every_landed_figure_is_metered(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger: Ledger = []

    async def wire(candidate: ImageCandidate, prompt: str, size: str, n: int) -> list[bytes]:
        return [b"png"]

    monkeypatch.setattr(figures, "wire_for", lambda _p: wire)
    landed = asyncio.run(
        figures.generate_batch(
            [("fig-a", "pa"), ("fig-b", "pb"), ("fig-c", "pc")], [_candidate("openai", ledger)]
        )
    )
    assert sorted(f for f, _ in landed) == ["fig-a", "fig-b", "fig-c"]
    assert ledger == [("openai", {"image": 1, "request": 1})] * 3


def test_the_chain_falls_through_and_only_the_buyer_is_metered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger: Ledger = []

    async def wire(candidate: ImageCandidate, prompt: str, size: str, n: int) -> list[bytes]:
        if candidate.provider == "xai":
            raise RuntimeError("404 - stale id")
        return [b"png"]

    monkeypatch.setattr(figures, "wire_for", lambda _p: wire)
    landed = asyncio.run(
        figures.generate_batch([("fig-a", "p")], [_candidate("xai", ledger), _candidate("openai", ledger)])
    )
    assert len(landed) == 1
    assert ledger == [("openai", {"image": 1, "request": 1})]  # the failed candidate billed nothing


def test_an_empty_wire_answer_is_not_a_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger: Ledger = []

    async def wire(candidate: ImageCandidate, prompt: str, size: str, n: int) -> list[bytes]:
        return []

    monkeypatch.setattr(figures, "wire_for", lambda _p: wire)
    landed = asyncio.run(figures.generate_batch([("fig-a", "p")], [_candidate("openai", ledger)]))
    assert landed == [] and ledger == []


# ── the second sweep: bounded, stragglers only, money-guarded ─────────────────────────────────


def test_second_sweep_retries_exactly_the_stragglers_once(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger: Ledger = []
    attempts = {"pa": 0, "pb": 0}

    async def wire(candidate: ImageCandidate, prompt: str, size: str, n: int) -> list[bytes]:
        attempts[prompt] += 1
        if prompt == "pb" and attempts["pb"] == 1:
            raise RuntimeError("429 - rate window")
        return [b"png"]

    monkeypatch.setattr(figures, "wire_for", lambda _p: wire)
    landed = asyncio.run(
        figures.generate_batch([("fig-a", "pa"), ("fig-b", "pb")], [_candidate("openai", ledger)], breath_s=0)
    )
    assert {f for f, _ in landed} == {"fig-a", "fig-b"}
    assert attempts == {"pa": 1, "pb": 2}  # ONE bounded retry pass, stragglers only
    assert len(ledger) == 2


def test_the_money_guard_can_refuse_the_second_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger: Ledger = []
    attempts = {"pa": 0}

    async def wire(candidate: ImageCandidate, prompt: str, size: str, n: int) -> list[bytes]:
        attempts["pa"] += 1
        raise RuntimeError("provider down")

    async def parked() -> bool:
        return False  # the task parked or died during sweep one - buy nothing more

    monkeypatch.setattr(figures, "wire_for", lambda _p: wire)
    landed = asyncio.run(
        figures.generate_batch(
            [("fig-a", "pa")], [_candidate("openai", ledger)], breath_s=0, sweep_allowed=parked
        )
    )
    assert landed == [] and ledger == [] and attempts["pa"] == 1


def test_a_hung_provider_costs_one_timeout_not_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger: Ledger = []
    monkeypatch.setattr(figures, "IMAGE_TIMEOUT_S", 0.02)

    async def wire(candidate: ImageCandidate, prompt: str, size: str, n: int) -> list[bytes]:
        if candidate.provider == "google":
            await asyncio.sleep(1)  # hangs far past the belt
        return [b"png"]

    monkeypatch.setattr(figures, "wire_for", lambda _p: wire)
    landed = asyncio.run(
        figures.generate_batch([("fig-a", "p")], [_candidate("google", ledger), _candidate("openai", ledger)])
    )
    assert [f for f, _ in landed] == ["fig-a"]
    assert ledger == [("openai", {"image": 1, "request": 1})]


# ── the stale-writer + money guards, and the deterministic enqueue ────────────────────────────


def test_stale_reasons_cover_the_money_guard() -> None:
    running = cast(Task, SimpleNamespace(turn=2, state=TaskStatus.RUNNING, status="running"))
    parked = cast(Task, SimpleNamespace(turn=2, state=TaskStatus.WAITING, status="waiting"))
    stale = figures._stale_reason  # pyright: ignore[reportPrivateUsage]
    assert stale(None, 1) == "the task is gone"
    assert stale(running, 1) == "the turn moved on"
    assert stale(parked, 2) is not None  # a parked task buys nothing
    assert stale(running, 2) is None


def test_enqueue_id_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []

    class _Dispatcher:
        async def enqueue(self, job: str, job_id: str, **kwargs: object) -> None:
            sent.append((job, job_id))

    import prismoid.operations.dispatch as dispatch

    monkeypatch.setattr(dispatch, "get_dispatcher", lambda: _Dispatcher())
    tid = uuid.UUID("00000000-0000-0000-0000-00000000abcd")
    asyncio.run(figures.enqueue_pregeneration(tid, 3))
    assert sent == [("pregenerate_figures", f"figures-{tid}-t3")]


def test_prompt_is_deterministic_composition() -> None:
    outline = SimpleNamespace(continuity_bible="Calm, factual voice.\nrest", title="Harbors")
    prompt = figures._prompt(  # pyright: ignore[reportPrivateUsage]
        cast(Any, outline), "a crane at dusk", "Cranes"
    )
    assert "a crane at dusk" in prompt and "Cranes" in prompt
    assert "Harbors" in prompt and "Calm, factual voice." in prompt
