# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Research reaches the web on the device (ADR-078 decision 20). A live run's "find the five
best-reviewed dungeon crawler books" reached a writer and came back written from memory:
with no search engine the browser is the one road to the web, and nothing told the planner so. The
fix is one fact in what the Lead plans from. A cloud Machine's own rendering of it is `test_web_reach.py`'s."""

from __future__ import annotations

import sys
import uuid
from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.providers.search import search_chain
from prismoid.conf import settings
from prismoid.db.models.task import Task
from prismoid.operations import planning_brief
from prismoid.operations.execution import lead_rung
from prismoid.operations.planning_brief import WebReach
from prismoid.operations.planning_steps import TurnAsk
from prismoid.operations.turn.intake import Intake
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.lead import LeadFacts, TurnOrigin
from prismoid.schemas.model_class import ModelClass
from tests.support import web_reach as web
from tests.support.web_reach import SEATED, plannable, row

GOAL = "find the five best-reviewed dungeon crawler books and make me a list"
FACTS = LeadFacts(turn=1, origin=TurnOrigin.WEB)


def _task(driver: str | None, goal: str = GOAL, turn: int = 1) -> Task:
    return Task(
        id=uuid.uuid4(),
        goal=goal,
        turn=turn,
        clarify="auto",
        machine_driver=driver,
        answers=None,
    )


def _brief(web: WebReach) -> str:
    """What the Lead plans from beside the thread, on `web`'s road, with its turn note last."""
    return lead_rung.tail(lead_rung.lead_turn(FACTS, [], skills={}, reach=web), "")


@pytest.fixture(autouse=True)
def a_mac_that_can_run_the_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chrome where the step will look (any executable: CI has no Chrome), and a key for the row."""
    monkeypatch.setattr(settings.local, "chrome_path", sys.executable)
    monkeypatch.setattr(settings.worker, "anthropic_api_key", SecretStr("k"))


@pytest.fixture
def no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    monkeypatch.setattr(settings.search, "engines", "")


@pytest.fixture
def an_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    web.an_engine(monkeypatch)


def _told(task: Task, catalog: list[ModelSpec] = SEATED) -> bool:
    return planning_brief.web_reach(task, plannable(), catalog) is WebReach.DEVICE


@pytest.mark.usefixtures("no_engine")
def test_a_device_task_with_no_engine_is_told_the_web_is_the_computers() -> None:
    assert _told(_task(DEVICE_DRIVER))
    assert planning_brief.WEB_ON_COMPUTER.strip() in _brief(WebReach.DEVICE)


@pytest.mark.usefixtures("no_engine")
def test_the_fact_follows_the_roster_and_overrides_its_description() -> None:
    brief = _brief(WebReach.DEVICE)
    assert "Not for research" in brief, "the roster's words stand, for every other deployment"
    assert brief.index("# The roster") < brief.index("# Reading the web") < brief.index("# This turn")
    fact = planning_brief.WEB_ON_COMPUTER
    assert "`operator` step" in fact and "`researcher`" in fact and "`writer`" in fact


@pytest.mark.usefixtures("no_engine")
@pytest.mark.parametrize("driver", ["docker", "sandbar", "remote", None])
def test_a_cloud_task_never_hears_the_devices_fact(
    driver: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`None`: a task with no recorded driver is the worker's, here a cloud one. Its browser runs on
    the task's Machine, never "the user's own computer", so it hears the Machine's rendering."""
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    task = _task(driver)
    assert not _told(task)
    brief = _brief(planning_brief.web_reach(task, plannable(), SEATED))
    assert (
        planning_brief.WEB_ON_COMPUTER.strip() not in brief and planning_brief.WEB_ON_MACHINE.strip() in brief
    )


@pytest.mark.usefixtures("an_engine")
def test_a_device_with_a_search_engine_needs_no_fact() -> None:
    assert search_chain()
    assert not _told(_task(DEVICE_DRIVER))


def test_an_engine_missing_its_key_is_no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The road is asked of what `web_search` itself is built from, not what the config names."""
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    monkeypatch.setattr(settings.search, "engines", "brave")
    monkeypatch.setattr(settings.search, "keys", SecretStr(""))
    assert search_chain() == []
    assert _told(_task(DEVICE_DRIVER))


@pytest.mark.usefixtures("no_engine")
def test_no_browser_seat_tells_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No browsing agent, or the kill switch off, and the Lead is never sent to a seat it cannot use."""
    assert planning_brief.web_reach(_task(DEVICE_DRIVER), plannable(computer=False), SEATED) is WebReach.NONE
    monkeypatch.setattr(settings.computer_use, "enabled", False)
    assert not _told(_task(DEVICE_DRIVER))


@pytest.mark.usefixtures("no_engine")
def test_no_model_for_the_seat_tells_nothing_and_the_goal_still_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local user on a provider outside the seeded rows: `build("operator")` would raise, and a
    `writer` still works. Nothing is said of a road, so a research goal does not fail."""
    task = _task(DEVICE_DRIVER)
    assert not _told(task, [row(ModelClass.FAST_EXECUTOR)])
    assert not _told(task, [])
    monkeypatch.setattr(settings.worker, "anthropic_api_key", SecretStr(""))
    assert not _told(task), "a row this host cannot build is no seat either"
    assert "# Reading the web" not in _brief(WebReach.NONE)


@pytest.mark.usefixtures("no_engine")
def test_no_chrome_tells_nothing_and_the_goal_still_delivers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`NoChrome` stops a browser step with no fallback, and the setting the step reads is here too:
    told, a research goal would fail where it used to deliver."""
    task = _task(DEVICE_DRIVER)
    assert _told(task)
    monkeypatch.setattr(settings.local, "chrome_path", "/nowhere/Google Chrome")
    assert not _told(task)
    assert "# Reading the web" not in _brief(WebReach.NONE)


@pytest.mark.parametrize(("driver", "told"), [(DEVICE_DRIVER, True), ("docker", False)])
@pytest.mark.usefixtures("no_engine")
async def test_the_leads_brief_carries_the_fact(
    monkeypatch: pytest.MonkeyPatch, driver: str, told: bool
) -> None:
    """The real `lead_rung.brief`, past everything but its reads: the one call that carries the fact."""

    async def intake(*_args: object, **_kw: object) -> Intake:
        return Intake(facts=FACTS, bypass=None)

    async def asked(*_args: object, **_kw: object) -> TurnAsk:
        return TurnAsk()

    async def nobody(*_args: object, **_kw: object) -> list[object]:
        return []

    async def no_skills(*_args: object, **_kw: object) -> lead_rung.Skills:
        return lead_rung.Skills(needs={}, described={})

    monkeypatch.setattr(lead_rung.turn_intake, "intake", intake)
    monkeypatch.setattr(lead_rung, "turn_ask", asked)
    monkeypatch.setattr(lead_rung, "resolved_bots", nobody)
    monkeypatch.setattr(lead_rung, "published_skills", no_skills)
    deps, session = TaskDeps(), cast(AsyncSession, MagicMock())  # every read is stubbed above
    turn = await lead_rung.brief(
        session, _task(driver), deps, asked=False, catalog=SEATED, order={}, settings={}
    )
    assert (planning_brief.WEB_ON_COMPUTER.strip() in turn.roster) is told
    assert (planning_brief.WEB_ON_MACHINE.strip() in turn.roster) is not told, "a cloud Machine hears its own"
    assert deps.lead_facts == FACTS


@pytest.mark.usefixtures("no_engine")
def test_a_researcher_stays_seatable_for_what_is_attached() -> None:
    """The fact stops a researcher being sent to the web; reducing attached sources it still does."""
    assert "## researcher" in _brief(WebReach.DEVICE)
    assert "never seat one to find things out" in planning_brief.WEB_ON_COMPUTER
    assert "`maker`, `researcher`, `writer` read only what is attached" in planning_brief.WEB_ON_COMPUTER


@pytest.mark.usefixtures("no_engine")
def test_a_connector_still_serves_what_it_covers() -> None:
    """A request a connector covers is its tools' work, and the browser's fact must not take it."""
    assert "An attached connector still serves what it covers" in planning_brief.WEB_ON_COMPUTER


@pytest.mark.usefixtures("no_engine")
def test_a_follow_up_hears_it_too() -> None:
    later = lead_rung.lead_turn(
        LeadFacts(turn=3, origin=TurnOrigin.WEB), [], skills={}, reach=WebReach.DEVICE
    )
    assert planning_brief.WEB_ON_COMPUTER.strip() in later.roster
