# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device helper's wire - values only, no I/O.

The worker speaks these to `prismoid device helper` over a Unix socket. `FramePathsWire` is the wire
twin of `ai/frames/layout.FramePaths`: `prismoid.device` may not import `prismoid.ai`, so the helper
names where a frame's roots really are and the worker rebuilds its `FramePaths` from this.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field

from prismoid.schemas.device_away import DEVICE_AWAY as DEVICE_AWAY
from prismoid.schemas.device_grants import WORK_NAME as WORK_NAME
from prismoid.schemas.device_grants import FolderGrant
from prismoid.schemas.device_grants import folder_key as folder_key
from prismoid.schemas.device_grants import grant_name_ok as grant_name_ok
from prismoid.schemas.device_grants import named_grants as named_grants
from prismoid.schemas.device_grants import write_root_refusal as write_root_refusal
from prismoid.schemas.machines import FrameDriverName, FrameKind

# What a Machine on the device records as its driver (`tasks.machine_driver`, `machines.driver`).
DEVICE_DRIVER: Final = FrameDriverName.DEVICE

# Prismoid's own Python, in the home: a uv-made venv pinned to the image's versions, with the
# house style in its site-packages. The stamp is written last: no stamp, no runtime.
RUNTIME_DIR: Final = "runtime"
RUNTIME_STAMP: Final = "stamp"
# The variable the runtime's Python is spelled with, on every command: the helper knows where its
# home is, and the journal keeps the word, never a path of the user's disk.
SYSTEM_PYTHON: Final = "PRISMOID_PYTHON"
# Set on every command while LibreOffice is off (`settings.local.has_office`): the house's
# `render.to_pdf` reads it and keeps to what needs no conversion. Never set on a cloud frame.
NO_RENDITIONS: Final = "PRISMOID_NO_RENDITIONS"
# Where the optional tools live (poppler's `pdftoppm`), after the OS's own folders on a system
# command's PATH: Homebrew's two prefixes, outside the user's home, so never a folder the agent can
# write.
TOOL_DIRS: Final[tuple[str, ...]] = ("/opt/homebrew/bin", "/usr/local/bin")
# What the runtime is called in anything stored: a traceback through its site-packages names it.
RUNTIME_SHOWN: Final = "/runtime"
# Docker's honest limit (spec 6.7, 7.1), said wherever the grant is given.
DOCKER_NOTE: Final = (
    "Docker's containers reach the network directly: their egress is not fenced by Prismoid's allowlist."
)
RUNTIME_MISSING: Final = (
    "Prismoid's own Python runtime is missing on this computer, and this file type needs it: run "
    "`prismoid down`, then `prismoid up` with a network connection to build it, and publish again"
)


def runtime_python(home: Path) -> Path:
    """The runtime's interpreter under the Prismoid home `home`, whether it is there or not."""
    return home / RUNTIME_DIR / "venv" / "bin" / "python"


# A task id becomes a folder name under the helper's home: no separators, no dots.
TaskId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class GrantsIn(BaseModel):
    """A frame's grants, replaced (`PUT /frames/{ref}/grants`): what Postgres holds for the task now."""

    grants: list[FolderGrant] = []
    docker: bool = False


class FrameSpecIn(BaseModel):
    task_id: TaskId
    kind: FrameKind
    grants: list[FolderGrant] = []
    docker: bool = False
    allow_domains: list[str] = []
    egress_version: int = 1
    # "browser": the task drives the Prismoid Chrome - the helper opens the frame a window.
    profile: Literal["base", "browser"] = "base"
    # The provider labels the reconciler judges the frame by; the helper keeps and lists them.
    labels: dict[str, str] = {}


class FramePathsWire(BaseModel):
    task: str
    work: str
    artifacts: str
    skills: str
    verify: str
    inputs: str
    browser: str
    out: str
    # Every granted folder but the work folder: its virtual root (`FolderGrant.name`) -> its real path.
    grants: dict[str, str] = {}


class FrameOut(BaseModel):
    ref: str
    paths: FramePathsWire
    device_name: str
    labels: dict[str, str] = {}
    created_at: dt.datetime | None = None
    # What the frame was granted, as the helper holds it: the worker re-pushes when it differs.
    grants: list[FolderGrant] = []
    docker: bool = False
    # Each real root that needs quoting in a command, and the link that does not (`frames.aliases`).
    aliases: dict[str, str] = {}


class ExecIn(BaseModel):
    exec_id: str
    command: str
    timeout: float = Field(allow_inf_nan=False)
    system: bool = False


class ExecOut(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


class FileIn(BaseModel):
    path: str
    data_b64: str
    mode: int = 0o644
    owner: Literal["agent", "system"] = "agent"
    # Only the work folder and `out/`, by where the path really goes (`DeviceFrame.save_output`).
    confined: bool = False


# What the file door says of a link it will not follow, which a caller tells from other refusals.
LINK_REFUSED: Final = "a link in the path"


class PathIn(BaseModel):
    path: str
    # Read: refuse a link at any step of the path, instead of resolving it (a directory tar).
    nofollow: bool = False


class FileDataOut(BaseModel):
    data_b64: str


class DirOut(BaseModel):
    names: list[str]


class SignalIn(BaseModel):
    signal: Literal["stop", "cont"]


class EgressIn(BaseModel):
    """A frame's egress list, replaced (`FrameDriver.allow`): stale `version` is a 409."""

    domains: list[str]
    version: int | None = None


class InterruptedOut(BaseModel):
    exec_id: str
    command_head: str
    started_at: dt.datetime
    # True: the command still runs in this helper life (only the worker went away). False: the
    # helper died under it, and what it left running was killed.
    still_running: bool = False
    # Set: it finished, with this exit code, after its worker had gone, so nobody saw its result.
    exit_code: int | None = None


class InterruptedAckIn(BaseModel):
    exec_id: str
