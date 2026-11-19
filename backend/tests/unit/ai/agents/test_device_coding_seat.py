# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A coding task on the device (spec O11): the coding seat tests the running app in the
task's window and delivers a `change`. Both follow from the task's driver alone, in code, and a
cloud task's seats, tools and plans stay what they were."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from prismoid.ai.agents import base
from prismoid.ai.agents.base import build, definition_for, frame_profile_for, get
from prismoid.ai.context.brief import prompt_for
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway import providers
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.tools.browser import BROWSER_TOOLS, TOOL_NAMES, BrowserPorts
from prismoid.conf import settings
from prismoid.db.models.task import Task
from prismoid.errors import PolicyDenied
from prismoid.operations import execution, machines, planning_steps
from prismoid.operations.plan_shapes import one_step_plan
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import AgentName, SkillName, TaskId
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import StepSpec

DEVICE_TASK = Task(machine_driver="device")
CLOUD_TASK = Task(machine_driver=None)


def coding_plan() -> Any:
    steps, _edges, _criteria = one_step_plan("Fix the login", agent="maker", produces=ArtifactType.CODE)
    return steps


def _typed(steps: Any, task: Task) -> Any:
    return planning_steps.typed_for_machine(steps, task)


async def _browse(_cmd: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def _allow(_domains: list[str]) -> bool:
    return True


def deps(*, ports: BrowserPorts | None) -> TaskDeps:
    """A coding step's deps: the `code` format skill pinned, which seats the maker in the code class."""
    return TaskDeps(
        task_id=TaskId(uuid.uuid4()), browser=ports, skill_pins=[SkillPin(name=SkillName("code"), version=1)]
    )


@pytest.fixture(autouse=True)
def cloud_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    monkeypatch.setattr(settings.computer_use, "enabled", True)


def test_a_device_plan_types_the_coding_step_change() -> None:
    [(_id, spec)] = _typed(coding_plan(), DEVICE_TASK)
    assert spec.contract.output.type is ArtifactType.CHANGE
    told = prompt_for(spec)
    assert "/out/change" in told and "type='change'" in told and "type='code'" not in told


def test_a_cloud_plan_is_the_one_it_drafted() -> None:
    steps = coding_plan()
    assert _typed(steps, CLOUD_TASK) is steps
    [(_id, spec)] = steps
    assert spec.contract.output.type is ArtifactType.CODE
    told = prompt_for(spec)
    assert "`/work`" in told and "type='code'" in told and "/out/change" not in told


def test_only_a_coding_step_is_retyped_on_the_device() -> None:
    steps, _edges, _criteria = one_step_plan("Write a memo")
    [(_id, kept)] = steps
    [(_id2, spec)] = _typed(steps, DEVICE_TASK)
    assert spec is kept


def test_a_worker_on_the_device_counts_with_no_task_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """`machines.driver_of` is the rule: the task's own driver, else this worker's."""
    monkeypatch.setattr(settings.worker, "frame_driver", "device")
    [(_id, spec)] = _typed(coding_plan(), CLOUD_TASK)
    assert spec.contract.output.type is ArtifactType.CHANGE


def test_the_seat_boots_the_browser_only_on_the_device() -> None:
    [(_id, coding)] = coding_plan()
    assert machines.seat_profile(DEVICE_TASK, coding) == "browser"
    assert machines.seat_profile(CLOUD_TASK, coding) == "code"  # the code skill's image off the device
    assert frame_profile_for("maker") == "base"
    assert machines.seat_profile(DEVICE_TASK, StepSpec(agent=AgentName("writer"))) == "base"
    assert machines.seat_profile(CLOUD_TASK, StepSpec(agent=AgentName("operator"), browser=True)) == "browser"
    assert machines.seat_profile(CLOUD_TASK, StepSpec(agent=AgentName("operator"))) == "base"
    assert machines.seat_profile(DEVICE_TASK, StepSpec(agent=AgentName("a-bot"))) == "base"


def test_a_device_browser_step_without_chrome_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """No fallback: the step fails with the reason, before any frame, and never a raw 503 (which
    would read as the device gone away). The sentence names no path of the computer."""
    monkeypatch.setattr(settings.local, "chrome_path", "/no/such/chrome")
    with pytest.raises(PolicyDenied, match="Google Chrome") as refused:
        execution._check_chrome(DEVICE_TASK, "browser")  # pyright: ignore[reportPrivateUsage]
    assert "/no/such" not in str(refused.value)


def test_a_cloud_step_and_a_base_step_never_ask_for_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.local, "chrome_path", "/no/such/chrome")
    execution._check_chrome(CLOUD_TASK, "browser")  # pyright: ignore[reportPrivateUsage]
    execution._check_chrome(DEVICE_TASK, "base")  # pyright: ignore[reportPrivateUsage]


def test_the_kill_switch_leaves_the_maker_working_without_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser profile would be refused dispatch (`_check_computer_use`), and a coding task
    needs no browser to run: off, the seat is its plain self."""
    monkeypatch.setattr(settings.computer_use, "enabled", False)
    assert machines.seat_profile(DEVICE_TASK, StepSpec(agent=AgentName("maker"))) == "base"


def test_the_maker_on_the_device_holds_the_browser_tools() -> None:
    ports = BrowserPorts(browse=_browse, allow=_allow)
    seated = definition_for("maker", deps(ports=ports))
    assert set(BROWSER_TOOLS) <= set(seated.tools)
    assert set(get("maker").tools) <= set(seated.tools)


def test_the_maker_on_the_cloud_does_not() -> None:
    seated = definition_for("maker", deps(ports=None))
    assert seated.tools == get("maker").tools
    assert not set(BROWSER_TOOLS) & set(seated.tools)


def _tools_of(agent: Any) -> dict[str, Any]:
    """Every function tool the built agent really carries."""
    return {name: tool for ts in agent.toolsets if hasattr(ts, "tools") for name, tool in ts.tools.items()}


def _catalog(monkeypatch: pytest.MonkeyPatch) -> list[ModelSpec]:
    monkeypatch.setattr(providers, "api_key_for", lambda provider: "a-key")
    return [
        ModelSpec(
            provider="anthropic",
            model_id="m",
            family=Family("anthropic"),
            protocol="anthropic",
            classes=(ModelClass.CODE,),
        )
    ]


async def _save(path: str, _data: bytes) -> str:
    return path


def test_build_gives_the_device_maker_the_tools_and_save_as(monkeypatch: pytest.MonkeyPatch) -> None:
    ports = BrowserPorts(browse=_browse, allow=_allow, save=_save)
    tools = _tools_of(build("maker", deps(ports=ports), _catalog(monkeypatch)).agent)
    assert set(tools) >= TOOL_NAMES
    assert "save_as" in tools["browser_screenshot"].tool_def.parameters_json_schema["properties"]


def test_build_gives_the_cloud_maker_none(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools_of(build("maker", deps(ports=None), _catalog(monkeypatch)).agent)
    assert not TOOL_NAMES & set(tools)
    assert "ask_user" in tools  # the listing does see this agent's toolsets


def test_the_seat_stays_a_publisher_of_a_file() -> None:
    """The browser never makes it the computer: its contract still demands a publish, and no
    outcome stands in for one."""
    seated = definition_for("maker", deps(ports=BrowserPorts(browse=_browse, allow=_allow)))
    assert seated.publishes and not seated.delivers_outcome


def test_the_registry_itself_is_untouched() -> None:
    """`GET /v1/agents` and the planner's vocabulary read the registry: no device word in them."""
    maker = base.REGISTRY[get("maker").name]
    assert not set(BROWSER_TOOLS) & set(maker.tools) and maker.frame_profile == "base"
    operator = get("operator")
    assert operator.delivers_outcome and not operator.device_browser
