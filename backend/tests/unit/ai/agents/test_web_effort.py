# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Research depth follows the deliverable : a step reads as much of the web as its task's size
bucket allows. "write a 200-word memo comparing makemytrip and cleartrip" at the deployment's whole
per-step caps ran 8 searches and 15 fetches in 348 s for $0.22. A brief deliverable gets a few searches and fetches; every other size keeps the deployment's caps. Pure: no database, no model, no network."""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.ai.agents import mounts
from prismoid.ai.agents.base import get as agent_named
from prismoid.ai.agents.mounts import Mount
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.meter import MeterContext
from prismoid.conf import settings
from prismoid.schemas.sizing import WEB_EFFORT, SizeBucket, WebEffort, web_effort_for

B = SizeBucket


def test_only_a_brief_deliverable_reads_less_than_the_deployment_allows() -> None:
    """Brief is cut on evidence (the memo: 348 s → 122 s, the answer as good); standard - the size
    most steps are stamped - keeps the deployment's caps until an eval shows a cut costs nothing."""
    brief = web_effort_for("brief")
    assert brief is not None
    assert brief.searches < settings.search.max_per_step
    assert brief.fetches < settings.worker.fetch_max_per_step
    for deep in ("standard", "extended", "long", "book"):
        assert web_effort_for(deep) is None, f"{deep}: the deployment's caps"
    assert web_effort_for("auto") is None and web_effort_for(None) is None, "unresolved: the caps"
    assert set(WEB_EFFORT) == {B.BRIEF}


def _mount(effort: WebEffort | None, agent: str = "researcher") -> Mount:
    return Mount(
        definition=agent_named(agent),
        deps=TaskDeps(web_effort=effort),
        catalog=[],
        meter=MeterContext(),
        served=None,
        order=None,
        quality=None,
    )


def test_the_fetch_mount_lowers_the_per_step_cap_to_the_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(granted: tuple[str, ...], journal: object, **kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(mounts, "fetch_toolset", fake)
    mounts.web_fetch(_mount(WebEffort(searches=3, fetches=4)))
    assert seen["per_step"] == 4
    mounts.web_fetch(_mount(None))
    assert seen["per_step"] is None, "no effort: the deployment's cap"


def test_the_search_mount_lowers_the_per_step_cap_to_the_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def fake(granted: tuple[str, ...], journal: object, chain: object, caps: Any, **kw: Any) -> None:
        seen.append(caps.max_per_step)

    monkeypatch.setattr(mounts, "search_toolset", fake)
    monkeypatch.setattr(mounts, "search_chain", list)
    mounts.web_search(_mount(WebEffort(searches=3, fetches=4)))
    mounts.web_search(_mount(WebEffort(searches=99, fetches=99)))
    mounts.web_search(_mount(None))
    cap = settings.search.max_per_step
    assert seen == [3, cap, cap], "the effort lowers the cap, never raises it"


def test_the_web_mounts_read_the_runs_wall_when_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """The step runner learns its wall after the agent is built: the tools read `deps.deadline`
    at call time, never a copy taken at mount."""
    seen: dict[str, Any] = {}

    def fake(granted: tuple[str, ...], journal: object, *a: Any, **kw: Any) -> None:
        seen.setdefault("deadlines", []).append(kw["deadline"])

    monkeypatch.setattr(mounts, "fetch_toolset", fake)
    monkeypatch.setattr(mounts, "search_toolset", fake)
    monkeypatch.setattr(mounts, "search_chain", list)
    m = _mount(None, agent="lead")
    mounts.web_fetch(m)
    mounts.web_search(m)
    m.deps.deadline = 123.0
    assert [read() for read in seen["deadlines"]] == [123.0, 123.0]
