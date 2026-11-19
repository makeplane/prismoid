# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""No blind re-run; waiting for the device.

A device step that the durable engine recovers was running commands in the user's own folder: it
never starts a fresh attempt on its own. It parks on a question-less PAUSE gate ("Waiting for
<device name>", `park_device_away`) until a person resumes the task, and the attempt they resume is
told which command was cut off (`Cut`, kept on the row as `record.OWED_CUT` until a prompt naming it
commits). A helper that stays away past the grace (`through_grace`), an attempt the helper died
under or its wall clock stopped (`cut_short`) park the same way. `__init__` imports these under
private names and keeps the call sites."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.context.brief import interruption_note
from prismoid.ai.frames.driver import FrameUnreachable, KeepsJournal, StagesCopies
from prismoid.ai.frames.layout import WORK
from prismoid.conf import settings
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import ModelIncapable, PolicyDenied
from prismoid.operations import gates, machines
from prismoid.operations.execution.record import OWED_CUT, hand_back, with_record
from prismoid.operations.machines import Acquired
from prismoid.schemas.device import DEVICE_DRIVER, InterruptedOut
from prismoid.schemas.device_away import DeviceAwayWhy, one_line
from prismoid.schemas.ids import StepId
from prismoid.schemas.machines import MachineProfile

# **The device's grace**: a helper that does not answer is asked again
# every `DEVICE_RETRY_S` until `DEVICE_GRACE_S` after the first miss, and only then is the device
# away and the step parked (`DeviceAway`). A Mac that slept a few minutes is a gap, not an absence.
DEVICE_GRACE_S = 90.0
DEVICE_RETRY_S = 5.0


class DeviceResume(NamedTuple):
    """What `execute_step` takes from a step a person resumed after it waited for its device."""

    gotchas: list[str]
    resume: dict[str, object] | None
    at_review: bool


class DeviceAway(Exception):
    """The step's device stayed unreachable past `DEVICE_GRACE_S`: the ladder parks the step."""


def on_device(task: Task) -> bool:
    """The task's frames are made on the user's own computer - the driver the lifecycle makes them
    on (`machines.driver_of`: the task's own, else this worker's)."""
    return machines.driver_of(task) == DEVICE_DRIVER


def stage_root(frame: object) -> str:
    """The folder the engine writes a file it is about to publish in. `/work` on a cloud frame. On
    the user's own computer `/work` is their real folder, and a name the planner or a title chose
    must never land on a file of theirs, so a frame naming a `staging_dir` takes it there; the
    published name is the file's basename either way."""
    staging = frame.staging_dir if isinstance(frame, StagesCopies) else None
    return str(staging).rstrip("/") if staging else WORK


class NoChrome(PolicyDenied):
    """A browser step on a Mac without the Prismoid Chrome: no model and no retry changes that."""


def check_chrome(task: Task, profile: str) -> None:
    """A browser step on the device needs the Prismoid Chrome, and has no fallback (a coding step tests
    what it built there). Said here, before `acquire`: past it the helper's refusal is a
    5xx, which reads as the device gone away. The sentence names no path of the computer."""
    if profile != MachineProfile.BROWSER or not on_device(task):
        return
    from prismoid.device.chrome import missing  # heavy: the helper's Chrome, never a cloud worker's

    if missing(settings.local.chrome_path):
        raise NoChrome(
            "this step needs Google Chrome, and it was not found on this computer: install Google "
            "Chrome, or set PRISMOID_LOCAL_CHROME_PATH to its executable, and run it again"
        )


def unreachable(failed: BaseException) -> bool:
    """A frame provider that did not answer, however the lifecycle wrapped it (an infra
    `ModelIncapable` raised from the `FrameUnreachable`)."""
    seen: BaseException | None = failed
    while seen is not None:
        if isinstance(seen, FrameUnreachable):
            return True
        seen = seen.__cause__
    return False


@dataclass(frozen=True, slots=True)
class Cut:
    """What a crash cut off on the device, owed to the first fresh attempt a person
    resumes: the head of the command the helper's journal named when the step parked, whether it
    still ran then (only the worker had gone), its exit code when it had finished with its result
    going nowhere (the same), and its exec id - or, when the helper could not be
    asked then (`look`), the journal is read from the frame that attempt binds. Durable from the
    park on: in the park's state, then on the row (`OWED_CUT`) until a prompt naming it commits."""

    head: str | None = None
    look: bool = False
    still_running: bool = False
    exec_id: str | None = None
    exit_code: int | None = None

    @classmethod
    def of(cls, found: InterruptedOut) -> Cut:
        return cls(
            head=one_line(found.command_head),
            still_running=found.still_running,
            exec_id=found.exec_id,
            exit_code=found.exit_code,
        )

    def state(self) -> dict[str, object]:
        return {
            "head": self.head,
            "look": self.look,
            "still_running": self.still_running,
            "exec_id": self.exec_id,
            "exit_code": self.exit_code,
        }

    @classmethod
    def restored(cls, state: object) -> Cut | None:
        if not isinstance(state, dict) or ("head" not in state and "look" not in state):
            return None
        head, exec_id, code = state.get("head"), state.get("exec_id"), state.get("exit_code")
        return cls(
            head=str(head) if head else None,
            look=bool(state.get("look")),
            still_running=bool(state.get("still_running")),
            exec_id=str(exec_id) if exec_id else None,
            exit_code=code if isinstance(code, int) else None,
        )

    def owed(self) -> bool:
        """Something to tell, or to look for."""
        return self.head is not None or self.look


def owed_state(step: PlanStep, cut: Cut | None) -> dict[str, Any] | None:
    """The row's state with `cut` owed to the next fresh attempt (None: nothing owed any more)."""
    state = {k: v for k, v in (with_record(step, None) or {}).items() if k != OWED_CUT}
    if cut is not None and cut.owed():
        state[OWED_CUT] = cut.state()
    return state or None


async def park_device_away(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    why: DeviceAwayWhy,
    seat: bool = False,
    cut: Cut | None = None,
    resume: dict[str, object] | None = None,
    gotchas: list[str] | None = None,
) -> str | None:
    """Park the step on a "Waiting for <device>" PAUSE gate until a person resumes, keeping the Machine,
    the cut command's evidence and what the step was owed. The command head never reaches a log or span."""
    still_owed = Cut.restored((step.resume_state or {}).get(OWED_CUT))
    await session.commit()  # no transaction waits on the helper, nor on the Machine's row
    if not seat:
        await machines.hold_parked(task.id, step.id, step.attempt)
    found: InterruptedOut | None = None
    if why == "interrupted" and cut is None and not seat:
        found, cut = await _read_cut_off(session, task, step)
    cut = parked_cut(found, still_owed, cut)
    machine = await Machine.for_task(session, task.id)
    name = machine.device_name if machine is not None else None

    task = await Task.held(session, task.id)  # the lock the pause doors write under
    if task.is_terminal():
        await session.commit()
        return None  # the caller gives the step's park back (`execute_step`'s ended-task doors)
    away = away_state(why, name=name, cut=cut, resume=resume, gotchas=gotchas)
    step.resume_state = with_record(step, {"device_away": away})
    gate = await gates.open_device_away(
        session,
        task,
        StepId(step.id),
        why,
        device_name=name,
        head=cut.head if cut is not None else None,
        still_running=cut is not None and cut.still_running,
        exit_code=cut.exit_code if cut is not None else None,
    )
    outcome = hand_back(step, f"park:{gate.id}")
    await session.commit()
    if found is not None:
        try:
            await machines.ack_interrupted(session, task.id, found.exec_id)
        except Exception as unacked:  # the next park reads the same entry again: honest, never lost
            logfire.warn(
                "step {s}: the device's journal was not told ({e})", s=str(step.id), e=type(unacked).__name__
            )
    logfire.info(
        "step {step} parked waiting for its device ({why}) - gate {gate}",
        step=str(step.id),
        why=why,
        gate=str(gate.id),
    )
    return outcome


async def _read_cut_off(
    session: AsyncSession, task: Task, step: PlanStep
) -> tuple[InterruptedOut | None, Cut | None]:
    """The helper's journal, asked which command the worker's going away cut off: what it named
    (None when nothing), or - when it could not be asked - a cut that leaves the resumed attempt to
    look."""
    try:
        return await machines.interrupted(session, task.id), None
    except RuntimeError:
        return None, None  # the helper has never heard of the frame: nothing of it survives to read
    except Exception as unread:
        logfire.warn(
            "step {s}: the device's journal could not be read ({e})",
            s=str(step.id),
            e=type(unread).__name__,
        )
        return None, Cut(look=True)


def parked_cut(found: InterruptedOut | None, still_owed: Cut | None, cut: Cut | None) -> Cut | None:
    """The cut a park keeps: what the journal just named, else a cut still owed from an earlier park
    unless a newer one was read (a look asked for now stays asked), else the cut it was given."""
    if found is not None:
        return Cut.of(found)
    if still_owed is not None and (cut is None or cut.head is None):
        return replace(still_owed, look=still_owed.look or (cut is not None and cut.look))
    return cut


def away_state(
    why: DeviceAwayWhy,
    *,
    name: str | None,
    cut: Cut | None,
    resume: dict[str, object] | None,
    gotchas: list[str] | None,
) -> dict[str, object]:
    """The park's `device_away` state: why, the device's name, the cut, and what the resumed
    attempt is owed (a run's `resume`, the last failure's gotchas)."""
    away: dict[str, object] = {"why": why, "name": name, **(cut.state() if cut is not None else {})}
    if resume is not None:
        away["resume"] = resume
    if gotchas:
        away["gotchas"] = gotchas
    return away


class DeviceCutShort(Exception):
    """A device attempt's wall clock stopped it: its command may still run on the
    device, so the step parks instead of buying another attempt (`cut_short`)."""


def wall_clock_stop(task: Task, wall_clock: float) -> Exception:
    """What an attempt's wall-clock stop raises: an infra rung - or, on the device, a
    park (`DeviceCutShort`). (Out of `_attempt` only to keep that function analysable.)"""
    if on_device(task):
        return DeviceCutShort(f"the step ran past its {int(wall_clock)}s wall clock on the device")
    return ModelIncapable(
        f"the step ran past its {int(wall_clock)}s wall clock and was stopped - "
        "the work so far is journalled; this costs one rung, not the task",
        infra=True,
    )


def cut_short(task: Task, failed: BaseException) -> bool:
    """A device attempt that stopped under its command: the helper went away while it
    ran (a `FrameUnreachable`, however wrapped), or its wall clock stopped it. Never re-run. The
    failure is read first: any other leaves `task` unread, which a rolled-back session expired."""
    return (isinstance(failed, DeviceCutShort) or unreachable(failed)) and on_device(task)


async def left_running(session: AsyncSession, task: Task, step: PlanStep) -> Cut | None:
    """The command a fresh attempt on the device would start beside, or None. An attempt
    can end under a command the helper runs on: a sibling's refusal in a parallel batch cancels only
    the worker's side of the call, and a handler is not cancelled when its client goes. The journal
    says so, and the ladder parks on it (`Cut.of`). Not on a command a person already resumed past
    (the owed cut names it, or says to look it up): the attempt is told it, which ends the entry. A
    helper that cannot be asked is `acquire`'s to find, next and through the grace; a frame it never
    heard of has nothing running. Any other failure to read parks, the attempt left to look; once:
    a person who resumed past that park is not parked on it again, or a read that stays broken would
    hold every resume. The owed cut is read first, while the row is still sound."""
    owed = Cut.restored((step.resume_state or {}).get(OWED_CUT))
    try:
        found = await machines.interrupted(session, task.id)
    except (RuntimeError, FrameUnreachable):
        return None
    except Exception as unread:
        logfire.warn(
            "step {s}: the device's journal could not be read ({e})",
            s=str(step.id),
            e=type(unread).__name__,
        )
        return None if owed is not None else Cut(look=True)
    if found is None or not found.still_running:
        return None
    return None if owed is not None and (owed.look or owed.exec_id == found.exec_id) else Cut.of(found)


async def owed_cut(frame: object, cut: Cut | None) -> Cut:
    """The cut-off command an attempt is told. When the helper could not be asked as the
    step parked (`Cut.look`), the journal is asked now, through the frame the resumed attempt bound;
    what it names is told, and the journal is told back only once the prompt naming it is recorded
    (`journal_told`). Never raises: a journal that cannot be read is an attempt told what the park
    knew. (Out of `_attempt` only to keep that function analysable.)"""
    if cut is None or not cut.look or not isinstance(frame, KeepsJournal):
        return cut or Cut()
    try:
        found = await frame.interrupted()
    except Exception:
        return replace(cut, look=False)
    return Cut.of(found) if found is not None else replace(cut, look=False)


def told_owed(step: PlanStep) -> None:
    """An attempt's receipt is about to tell the owed cut: it rides that commit off the row."""
    step.resume_state = owed_state(step, None)


def cut_note(told: Cut) -> str | None:
    """`owed_cut`'s cut as a continued run's line."""
    if told.head is None:
        return None
    return interruption_note(told.head, still_running=told.still_running, exit_code=told.exit_code)


async def journal_told(frame: object, told: Cut) -> None:
    """The prompt naming the cut-off command is on the record: the frame's journal stops naming it,
    and every older entry with it - again, for one the park already acked (idempotent: a park whose
    own ack was lost is covered here)."""
    if told.exec_id and isinstance(frame, KeepsJournal):
        with contextlib.suppress(Exception):  # a later park reads the same entry again: never lost
            await frame.ack_interrupted(told.exec_id)


async def through_grace(task: Task, step: PlanStep, acquire: Callable[[], Awaitable[Acquired]]) -> Acquired:
    """`_acquire_for`'s acquire, with the device's grace: on the device a helper that does not
    answer is waited for, inside `DEVICE_GRACE_S`; each failed `acquire` hands the Machine back as it
    found it, so asking again is the same claim. Past the grace the device is away (`DeviceAway`)."""
    missed: float | None = None  # when the helper first did not answer
    while True:
        try:
            return await acquire()
        except Exception as failed:
            if not (on_device(task) and unreachable(failed)):
                raise
            now = time.monotonic()
            missed = now if missed is None else missed
            left = missed + DEVICE_GRACE_S - now
            if left <= 0:
                raise DeviceAway(str(failed)) from failed
            logfire.warn("step {s}: the device did not answer; asking again", s=str(step.id))
            await asyncio.sleep(min(DEVICE_RETRY_S, left))
