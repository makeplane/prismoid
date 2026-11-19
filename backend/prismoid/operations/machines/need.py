# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Which steps of a plan take the task's Machine, and whether their work may need the computer.

A serial step starts on paper and takes the Machine on its first call that needs the computer
(`capabilities/machine_acquire.py`, ADR-063 amended), so a Machine starts for work, never for a
grant. This module answers the guess the prewarm makes (`early_boot.for_plan`): a committed plan
whose step holds a tool that changes the Machine (`needs_machine`: a shell, an image rendered into
`/work`) or runs on an image that is not the base one may need it soon, so its boot starts in the
background and nobody waits on it. A file the model writes does not (paper holds it, and a park
carries it to the Machine). No session, no row: a plan's specs in, a `PlanNeed` out, so the
prewarm, the plan's record and the problem book ask the same question.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from prismoid.ai.agents.base import browsing, frame_profile_for
from prismoid.ai.agents.base import get as agent_named
from prismoid.ai.capabilities.machine_acquire import needs_machine
from prismoid.db.models.task import Task
from prismoid.operations.machines.acquired import driver_of
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.machine_need import MachineNeed
from prismoid.schemas.machines import MachineProfile
from prismoid.schemas.plan import StepKind, StepSpec, is_seat


@dataclass(frozen=True, slots=True)
class PlanNeed:
    """The plan's need and the agents that decided it - agent names, never the prompt."""

    need: MachineNeed
    why: str


def needs_computer(tools: Iterable[str], profile: str) -> bool:
    """May a step holding `tools`, on an image of `profile`, need the computer for its work?"""
    return profile != MachineProfile.BASE or any(needs_machine(tool) for tool in tools)


def takes_machine(spec: StepSpec) -> bool:
    """A serial agent step binds the task's Machine. The express answer runs frameless, and a seat
    keeps a throwaway frame of its own."""
    return (
        spec.kind is StepKind.STEP
        and spec.agent is not None
        and spec.lane_role != "express"
        and not is_seat(spec)
    )


def agent_needs_computer(agent: str, *, browser: bool = False, device: bool = False) -> bool:
    """Read from the agent's grant and image for a step that drives the browser or not (`browser`). A bot
    compiles per task and its grant may hold a shell, so a name the registry does not know needs the computer."""
    try:
        definition = agent_named(agent)
    except KeyError:
        return True
    profile = frame_profile_for(agent, browser=browser, device=device)
    if definition.browses:
        definition = browsing(definition, on=profile == MachineProfile.BROWSER)
    return needs_computer(definition.tools, profile)


def plan_need(specs: Iterable[StepSpec], *, device: bool = False) -> PlanNeed:
    """What these steps ask of the task's Machine; one step that needs the computer decides it.
    `device`: the task runs on the person's own computer, where a coding seat also boots the browser."""
    takers = [
        (str(s.agent), agent_needs_computer(str(s.agent), browser=s.browser, device=device))
        for s in specs
        if takes_machine(s)
    ]
    if not takers:
        return PlanNeed(MachineNeed.NONE, "no step takes the task's Machine")
    computer = sorted({agent for agent, needs in takers if needs})
    if computer:
        return PlanNeed(MachineNeed.COMPUTER, f"the computer does the work of: {', '.join(computer)}"[:200])
    files = sorted({agent for agent, _ in takers})
    return PlanNeed(MachineNeed.FILES, f"no step's work needs the computer: {', '.join(files)}"[:200])


def plan_need_for(task: Task, specs: Iterable[StepSpec]) -> PlanNeed:
    """`plan_need` for this task's own driver."""
    return plan_need(specs, device=driver_of(task) == DEVICE_DRIVER)
