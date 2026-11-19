# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a plan may ask of the task's Machine (the prewarm's guess), read from the agents' tools, and
which steps start on paper. No I/O: the rules are pure, and they resolve the registry, never a list
of agent names."""

from __future__ import annotations

import pytest

from prismoid.ai.agents.base import roster
from prismoid.ai.tools.catalog import side_effect_of
from prismoid.conf import settings
from prismoid.db.models.task import Task
from prismoid.operations.machines import fits_on_paper, plan_need, starts_on_paper, takes_machine
from prismoid.operations.machines.need import needs_computer
from prismoid.schemas.machine_need import MachineNeed
from prismoid.schemas.plan import StepKind, StepSpec


def _spec(agent: str | None, **over: object) -> StepSpec:
    return StepSpec.model_validate({"title": "t", "agent": agent, **over})


@pytest.mark.parametrize(
    ("tools", "profile", "needs"),
    [
        ((), "base", False),
        (("frame.read_file", "web.fetch", "web.search", "request_connector"), "base", False),
        # Over the pages the run already read: nothing on the Machine.
        (("research.find_in_page", "research.note", "routine.create", "ask_user"), "base", False),
        # Publishing what the system mounted holds nothing across a park: paper serves it.
        (("frame.read_file", "frame.publish"), "base", False),
        # Paper holds a file the model writes; a park carries it to the Machine.
        (("frame.write_file",), "base", False),
        (("frame.edit",), "base", False),  # an edit is a write: paper holds it too
        (("frame.shell",), "base", True),
        (("media.generate_image",), "base", True),  # the image is rendered into the Machine's /work
        (("a_connector_write",), "base", False),  # a world act runs in the worker, not the Machine
        ((), "browser", True),  # the image itself is the computer
    ],
)
def test_the_rule_reads_tools_and_the_image(tools: tuple[str, ...], profile: str, needs: bool) -> None:
    assert needs_computer(tools, profile) is needs


def test_every_granted_tool_is_classed_by_the_catalog() -> None:
    """A granted tool the catalog does not class would take a Machine by default, unnoticed."""
    granted = {tool for agent in roster() for tool in agent.tools}
    assert not {tool for tool in granted if side_effect_of(tool) is None}


@pytest.mark.parametrize(
    ("agent", "needs"),
    [
        ("maker", True),
        ("researcher", False),  # it reads and searches; nothing it holds changes the Machine
        ("writer", False),  # reads its inputs; the system publishes its text
        ("operator", False),
        ("a-bot-the-registry-never-heard-of", True),  # a bot's grant may hold a shell
    ],
)
def test_a_one_step_plan_reads_its_agent(agent: str, needs: bool) -> None:
    need = plan_need([_spec(agent)])
    assert need.need is (MachineNeed.COMPUTER if needs else MachineNeed.FILES), need.why
    assert agent in need.why, "the record names the agent that decided it"


def test_a_connector_act_takes_no_machine_and_a_browser_step_does() -> None:
    """The operator off the browser holds no shell: a connector act runs on paper; on the browser it is the computer."""
    assert plan_need([_spec("operator")]).need is MachineNeed.FILES
    assert plan_need([_spec("operator", browser=True)]).need is MachineNeed.COMPUTER


def test_talk_and_seats_take_no_machine() -> None:
    assert not takes_machine(_spec("lead", lane_role="express"))
    assert not takes_machine(_spec("maker", model_ref="anthropic:x"))  # a fleet seat
    assert not takes_machine(_spec("writer", lane_role="section"))
    assert not takes_machine(_spec("researcher", lane_role="gather"))
    assert not takes_machine(_spec(None, kind=StepKind.GATE))
    assert plan_need([_spec("lead", lane_role="express")]).need is MachineNeed.NONE
    assert plan_need([]).need is MachineNeed.NONE


def test_one_computer_step_decides_the_plan() -> None:
    need = plan_need([_spec("researcher"), _spec("writer"), _spec("maker")])
    assert need.need is MachineNeed.COMPUTER
    assert "maker" in need.why and "writer" not in need.why and "researcher" not in need.why


def test_a_device_maker_needs_the_browser_image_too() -> None:
    assert plan_need([_spec("maker")], device=True).need is MachineNeed.COMPUTER


def _task(inputs_bytes: int = 0, driver: str | None = None) -> Task:
    inputs = [{"filename": "a.pdf", "sha256": "0" * 64, "bytes": inputs_bytes}] if inputs_bytes else []
    return Task(goal="g", inputs=inputs, mode="standard", machine_driver=driver)


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", "remote")
    monkeypatch.setattr(settings.machines, "paper", True)
    monkeypatch.setattr(settings.machines, "enabled", True)
    monkeypatch.setattr(settings.machines, "paper_max_bytes", 1000)


@pytest.mark.usefixtures("cloud")
@pytest.mark.parametrize(
    ("agent", "over", "paper"),
    [
        ("writer", {}, True),
        ("operator", {}, True),
        ("researcher", {}, True),
        ("maker", {}, True),  # it takes the Machine at its first shell, never before
        ("operator", {"browser": True}, False),  # a step that drives the browser boots it up front
        ("writer", {"lane_role": "express"}, False),  # the answer is frameless already
        ("writer", {"lane_role": "section"}, False),  # a seat keeps its throwaway frame
        ("a-bot-the-registry-never-heard-of", {}, True),  # a bot's shell takes the Machine on demand
    ],
)
def test_which_steps_start_on_paper(agent: str, over: dict[str, object], paper: bool) -> None:
    assert starts_on_paper(_task(), _spec(agent, **over), seat=False, has_machine=False) is paper


@pytest.mark.usefixtures("cloud")
def test_paper_yields_to_the_machine_when_it_should(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _spec("writer")
    assert not starts_on_paper(_task(), writer, seat=True, has_machine=False), (
        "a dispatched seat keeps its frame"
    )
    assert not starts_on_paper(_task(driver="device"), writer, seat=False, has_machine=False), (
        "the device costs no slot"
    )
    monkeypatch.setattr(settings.machines, "paper", False)
    assert not starts_on_paper(_task(), writer, seat=False, has_machine=False), (
        "the switch puts it back on the Machine"
    )
    monkeypatch.setattr(settings.machines, "paper", True)
    monkeypatch.setattr(settings.machines, "enabled", False)
    assert not starts_on_paper(_task(), writer, seat=False, has_machine=False), "the lever: a frame per step"


@pytest.mark.usefixtures("cloud")
def test_a_task_with_a_machine_binds_it_for_a_step_that_may_need_it() -> None:
    """A retry and a later step see the task's `/work`; a step that cannot need it stays on paper."""
    assert not starts_on_paper(_task(), _spec("maker"), seat=False, has_machine=True)
    assert starts_on_paper(_task(), _spec("writer"), seat=False, has_machine=True)


@pytest.mark.usefixtures("cloud")
def test_mounts_must_fit_in_half_the_cap() -> None:
    assert fits_on_paper(_task(inputs_bytes=300), [200])
    assert not fits_on_paper(_task(inputs_bytes=300), [201]), "past half the cap"
    assert not fits_on_paper(_task(), [400, 101]), "every mounted artifact counts"
