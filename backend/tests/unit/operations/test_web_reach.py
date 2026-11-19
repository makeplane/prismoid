# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How a task reaches the open web, as the Lead hears it (`planning_brief.web_reach`, `lead_rung.lead_turn`).

Found live (2026-11-10, a cloud Machine): "use current on-road prices ... and cite sources" was
planned as one spreadsheet step, which had no web tool and filled the workbook with its own estimates.
Now each deployment's road is named in what the Lead plans from: a search engine on the entries of the
agents that hold it, or the computer's browser - on the user's own Mac or on the task's Machine - and
nothing where no road exists."""

from __future__ import annotations

import dataclasses
import sys
import uuid

import pytest
from pydantic import SecretStr

from prismoid.ai.agents.base import get
from prismoid.conf import settings
from prismoid.db.models.task import Task
from prismoid.operations.execution import lead_rung
from prismoid.operations.planning_brief import (
    WEB_ON_COMPUTER,
    WEB_ON_MACHINE,
    WebReach,
    roster_entry,
    web_reach,
)
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.lead import LeadFacts, TurnOrigin
from tests.support import web_reach as web
from tests.support.web_reach import SEATED, entry, plannable

GOAL = "A spreadsheet of current on-road prices for three SUVs, and cite sources"
SEARCH_LINE = "Searches the web here"

# The device's fact, typed out, so the device's prompt cannot drift along with the template that
# renders both roads; the agents that read only named pages are the grants' (`web.fetch`).
DEVICE_FACT = (
    "# Reading the web\n\n"
    "This task runs on the user's own computer, and no search engine is set up here, so reading the "
    "web is the `operator`'s: it opens sites in a real browser and reads them. Anything the "
    "request needs from the web - research, finding, comparing, checking what is true now - is a plan "
    "with an `operator` step, whatever its roster entry says about research, and a document follows from "
    "it: the same step when it can write the file, or a `writer` step after it. No other agent can "
    "search the web here, and neither can you: `maker`, `researcher`, `writer` read only what is attached "
    "and URLs the request itself names, so never seat one to find things out, and never answer such a "
    "request from memory, in your reply or in a `writer` step. An attached connector still serves what it "
    "covers: a service it reaches is its tools' work, not the browser's.\n"
)
FACTS = LeadFacts(turn=1, origin=TurnOrigin.WEB)


def _task(driver: str | None) -> Task:
    return Task(id=uuid.uuid4(), goal=GOAL, turn=1, clarify="auto", machine_driver=driver, answers=None)


def _brief(web: WebReach) -> str:
    """What the Lead plans from beside the thread, on `web`'s road, with its turn note last."""
    return lead_rung.tail(lead_rung.lead_turn(FACTS, [], skills={}, reach=web), "")


@pytest.fixture(autouse=True)
def a_host_that_can_run_the_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chrome where a device step would look (CI has none), a key for the row, the computer on."""
    monkeypatch.setattr(settings.local, "chrome_path", sys.executable)
    monkeypatch.setattr(settings.worker, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    monkeypatch.setattr(settings.search, "engines", "")


@pytest.fixture
def an_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    web.an_engine(monkeypatch)


def test_the_device_fact_is_byte_identical() -> None:
    assert WEB_ON_COMPUTER == DEVICE_FACT
    assert web_reach(_task(DEVICE_DRIVER), plannable(), SEATED) is WebReach.DEVICE
    assert DEVICE_FACT.strip() in _brief(WebReach.DEVICE)


@pytest.mark.parametrize("driver", ["docker", "sandbar", "remote", None])
def test_a_cloud_machine_with_no_engine_hears_the_browser_is_the_road(driver: str | None) -> None:
    """`None`: a task with no recorded driver is the worker's, here a cloud one."""
    task = _task(driver)
    assert web_reach(task, plannable(), SEATED) is WebReach.MACHINE
    brief = _brief(WebReach.MACHINE)
    machine = WEB_ON_MACHINE
    assert machine.strip() in brief and DEVICE_FACT.strip() not in brief
    assert brief.index("# The roster") < brief.index("# Reading the web") < brief.index("# This turn")
    assert "neither can you" in machine and "in your reply" in machine, "the Lead itself cannot search"
    assert "own computer" not in machine and "this task's Machine" in machine
    assert "`maker`" in machine, "a spreadsheet follows a computer step on its own agent"
    assert SEARCH_LINE not in brief, "no engine: no agent is said to search"


@pytest.mark.usefixtures("an_engine")
@pytest.mark.parametrize("driver", ["docker", DEVICE_DRIVER])
def test_a_search_engine_is_named_on_the_entries_that_hold_it(driver: str) -> None:
    task = _task(driver)
    assert web_reach(task, plannable(), SEATED) is WebReach.SEARCH
    brief = _brief(WebReach.SEARCH)
    assert SEARCH_LINE in entry(brief, "researcher") and SEARCH_LINE in entry(brief, "writer")
    assert SEARCH_LINE in entry(brief, "maker")
    assert SEARCH_LINE not in entry(brief, "operator"), "its own entry says how it uses the web"
    assert "# Reading the web" not in brief


def test_the_line_is_derived_from_the_grant_and_the_chain_never_hardcoded() -> None:
    researcher = get("researcher")
    assert SEARCH_LINE not in roster_entry(researcher), "absent unless the chain is live"
    assert SEARCH_LINE in roster_entry(researcher, searches=True)
    writer = get("writer")
    ungranted = dataclasses.replace(writer, tools=tuple(t for t in writer.tools if t != "web.search"))
    assert SEARCH_LINE in roster_entry(writer, searches=True)
    assert SEARCH_LINE not in roster_entry(ungranted, searches=True), "it holds no web tool"
    assert SEARCH_LINE not in researcher.description


def test_no_seat_no_road_and_nothing_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `operator` browser step that cannot run could only fail: the Lead hears of no road."""
    task = _task("docker")
    assert web_reach(task, plannable(computer=False), SEATED) is WebReach.NONE
    assert web_reach(task, plannable(), []) is WebReach.NONE, "no catalog row serves the class"
    monkeypatch.setattr(settings.worker, "anthropic_api_key", SecretStr(""))
    assert web_reach(task, plannable(), SEATED) is WebReach.NONE, "a row this host cannot build"
    brief = _brief(WebReach.NONE)
    assert "# Reading the web" not in brief and SEARCH_LINE not in brief


def test_the_device_without_chrome_has_no_road(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.local, "chrome_path", "/nowhere/Google Chrome")
    assert web_reach(_task(DEVICE_DRIVER), plannable(), SEATED) is WebReach.NONE
    assert web_reach(_task("docker"), plannable(), SEATED) is WebReach.MACHINE, (
        "Chrome is the device's question"
    )
