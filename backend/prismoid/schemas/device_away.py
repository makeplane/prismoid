# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Waiting for the device: how a step's wait for its computer reads - the
gate's reason, its title, and what each viewer may see of it. Words only, no I/O."""

from __future__ import annotations

from typing import Final, Literal

# A PAUSE gate whose reason starts with this is a step waiting for its device: `interrupted` (it
# was running when Prismoid stopped, and never re-runs on its own) or `away` (the helper is not there).
DEVICE_AWAY: Final = "device_away:"
DeviceAwayWhy = Literal["interrupted", "away"]
# The task's last word when the device does not come back within `settings.local.away_limit_h`.
DEVICE_GONE: Final = "stopped: the device never came back"


def device_label(name: str | None) -> str:
    """The computer as a person reads it: its own name, else "your computer" (spec O13)."""
    return name or "your computer"


def away_title(name: str | None) -> str:
    return f"Waiting for {device_label(name)}"


def one_line(command: str) -> str:
    """A command head on one line: it rides inside backticks, in a gate and in a prompt."""
    return " ".join(command.split())


def away_reason(
    why: DeviceAwayWhy,
    *,
    name: str | None,
    head: str | None = None,
    still_running: bool = False,
    exit_code: int | None = None,
) -> str:
    """A device-away gate's reason: the `device_away:<why>` line the surfaces key on, a sentence for
    the person, and - only for a command the step's run cut off, or that finished unseen
    (`exit_code`) - that command. The command is the
    user's own text and may hold a secret: only the task's creator reads that line (`for_viewer`)."""
    where = device_label(name)
    if why == "away":
        return f"{DEVICE_AWAY}away\nPrismoid cannot reach {where}. The step waits; resume the task once it is back."
    lines = [
        f"{DEVICE_AWAY}interrupted",
        f"Prismoid stopped in the middle of this step on {where}. It will not run again on its own: "
        "check the folder, then resume the task.",
    ]
    if head is not None and exit_code is not None:
        lines.append(f"Finished while Prismoid was away (exit {exit_code}): `{one_line(head)}`")
    elif head is not None and still_running:
        lines.append(f"Still running when Prismoid came back: `{one_line(head)}`")
    elif head is not None:
        lines.append(f"Cut off: `{one_line(head)}`")
    return "\n".join(lines)


def gone_reason(reason: str, *, hours: int) -> str:
    """The task's last word when a device-away gate outlived `hours`: a device that never came back
    (`away`), or a step nobody resumed (`interrupted` - the device may well be back)."""
    if reason.startswith(f"{DEVICE_AWAY}interrupted"):
        return f"stopped: not resumed within {hours} hours"
    return DEVICE_GONE


def for_viewer(reason: str, *, creator: bool) -> str:
    """A gate reason as one viewer may read it: a device-away gate's command line is the creator's."""
    if creator or not reason.startswith(DEVICE_AWAY):
        return reason
    return "\n".join(reason.splitlines()[:2])
