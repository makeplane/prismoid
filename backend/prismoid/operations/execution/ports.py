# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The browser toolset's hands on a step's frame (`BrowserPorts`): the live allowlist, the control
lease a person at the screen holds, where the agent acted, and the device's own window.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import logfire
from pydantic import ValidationError

from prismoid.ai.frames.driver import FrameDriver, HandsOver, SavesOutput
from prismoid.ai.tools.browser import BrowserPorts
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import live, machines
from prismoid.schemas.events import ScreenAction, ScreenBox
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.screen import ControlState, ControlUnknown

LOOPBACK: frozenset[str] = frozenset({"127.0.0.1", "localhost"})


def ports_allowed(frame_allowed: set[str]) -> set[str]:
    """The toolset's allowlist (`BrowserPorts.allowed`): the frame's, plus loopback - the computer
    opens the server it just started in its own frame (only a browser-profile step builds
    `BrowserPorts`). Never the frame's proxy list
    (`frame.create`/`frame.allow`): loopback never goes through the proxy (Chromium bypasses it
    for loopback), and a proxy that honoured it would be reaching ITS OWN host."""
    return set(frame_allowed) | set(LOOPBACK)


async def lease_now(task_id: uuid.UUID) -> ControlState | None:
    """`BrowserPorts.control`: the task's live lease, before each browser call. A read
    that fails is tried once more (a pool timeout passes); failing again, it raises
    `ControlUnknown` and the toolset refuses that call. Unknown is never "nobody is driving": a
    snapshot taken mid private sign-in would put what the person typed into the model's context.
    The run itself goes on - the model may try again."""
    try:
        return await machines.screen.control_state(task_id)
    except Exception:
        logfire.warn("task {task}: the control lease could not be read; reading it again", task=str(task_id))
    try:
        return await machines.screen.control_state(task_id)
    except Exception as exc:
        logfire.warn(
            "task {task}: the control lease could not be read twice; the browser call is refused",
            task=str(task_id),
        )
        raise ControlUnknown from exc


async def device_control() -> ControlState | None:
    """`BrowserPorts.control` on a device Machine: the window is the
    person's own, on their own computer, so no lease exists and nobody is ever "driving" in the
    lease's sense - every browser call goes ahead (a click in the window while the agent acts is an
    accepted collision; the person's tool is Pause). Its presence is what lets `browser.handoff`
    park the step on a CONTROL gate, which the task's creator ends with Done (`gates.resolve`).
    Never reads a lease or touches the screen relay: a device Machine has neither."""
    return None


def control_port(
    acquired: machines.Acquired, task_id: uuid.UUID
) -> Callable[[], Awaitable[ControlState | None]] | None:
    """`BrowserPorts.control` for the step's frame: the lease on a Machine with a screen, the
    device's no-lease port on a device window, and None where nobody can take the browser over."""
    if acquired.screen:
        return functools.partial(lease_now, task_id)
    if acquired.device_window:
        return device_control
    return None


def save_port(frame: object) -> Callable[[str, bytes], Awaitable[str]] | None:
    """`BrowserPorts.save`: only a frame on the device keeps a screenshot for the agent."""
    return frame.save_output if isinstance(frame, SavesOutput) else None


def hand_over_port(frame: object) -> Callable[[], Awaitable[None]] | None:
    """`BrowserPorts.hand_over`: only a frame on the device gives a person its window."""
    return frame.hand_over if isinstance(frame, HandsOver) else None


def screen_hook(
    screen: bool, task_id: uuid.UUID, step_id: uuid.UUID, project_id: uuid.UUID | None
) -> Callable[[Mapping[str, Any]], Awaitable[None]] | None:
    """Only a frame people can watch (`Acquired.screen`) publishes where the agent acted."""
    if not screen:
        return None
    return screen_actions(TaskId(task_id), StepId(step_id), ProjectId(project_id) if project_id else None)


def browser_ports(
    frame: FrameDriver, acquired: machines.Acquired, allowed: set[str], task: Task, step: PlanStep
) -> BrowserPorts:
    """The browser toolset's hands on this step's frame."""

    return BrowserPorts(
        browse=frame.browser,
        # A live widen moves the Machine row's egress with the frame.
        allow=functools.partial(machines.widen, acquired),
        allowed=ports_allowed(allowed),
        snapshot_window=settings.computer_use.snapshot_window,
        # The park outlasted its hold and the Machine slept: the page is gone.
        page_reset=acquired.woke_from_park,
        # A person driving the Machine's browser parks the next call. Only a
        # frame with a screen has a lease: a seat's throwaway browser, or a Machine on a
        # driver with no screen door, has none, and its
        # `browser.handoff` answers that nobody can take over - except the device,
        # whose window the person is looking at: its hand-off waits for their Done.
        control=control_port(acquired, task.id),
        # Where the agent acts, for the people watching the screen.
        on_action=screen_hook(acquired.screen, task.id, step.id, task.project_id),
        # A frame on the device files screenshots in the deliverable.
        save=save_port(frame),
        hand_over=hand_over_port(frame),
        # An empty page names the hosts the sandbox refused (a read: harvest still journals them).
        denials=frame.denials,
    )


def screen_actions(
    task_id: TaskId, step_id: StepId, project_id: ProjectId | None
) -> Callable[[Mapping[str, Any]], Awaitable[None]]:
    """`BrowserPorts.on_action`: each acting verb that succeeded, published as a Lane-2
    `screen.action` so everyone watching the screen sees where the agent acted. Host and path only
    - never a query string, which can carry a token (the `fetch_label` law); the path is capped so
    the frame stays under NOTIFY's ceiling. A box that is not one (a malformed sidecar answer) is
    dropped, never published half-shaped."""

    async def publish(action: Mapping[str, Any]) -> None:
        try:
            parts = urlsplit(str(action.get("url") or ""))
            # Only a web page has a host and path to name: a data:/javascript:/about: URL's
            # "path" is page content or script, never published.
            web = parts.scheme in ("http", "https")
            host, path = (parts.hostname or "", parts.path or "") if web else ("", "")
        except ValueError:
            host, path = "", ""
        raw = action.get("box")
        try:
            box = ScreenBox.model_validate(raw) if raw is not None else None
        except ValidationError:
            box = None
        await live.notify(
            ScreenAction(
                task_id=task_id,
                step_id=step_id,
                project_id=project_id,
                at=utcnow(),
                verb=str(action.get("verb", "")),
                box=box,
                host=host,
                path=path[:512],
            )
        )

    return publish
