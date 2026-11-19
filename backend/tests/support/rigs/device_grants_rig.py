# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the device-grant tests, shared by the
`test_device_grants*` files: a worker whose driver resolves to `device`, and a folder in the person's
home to grant. A plain module, not a test file."""

from __future__ import annotations

import atexit
import os
import pwd
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.user import User
from prismoid.operations import device_grants
from prismoid.schemas.device import FolderGrant
from tests.support.cast import bound_scope, member

USER_HOME = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))


@pytest.fixture(autouse=True)
def device_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """New tasks run on the device (`driver_of`), outside local mode: members stay possible."""
    monkeypatch.setattr(settings.worker, "frame_driver", "device")
    monkeypatch.setattr(settings.machines, "wait_s", 5)


def home_folder(prefix: str = "prismoid-e1-") -> Path:
    """A folder in the user's real home: a grant must be in the home (or on an external disk).
    Removed at the latest when the test process exits, should a test die before its own cleanup."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=USER_HOME))
    atexit.register(shutil.rmtree, path, True)
    return path


@pytest.fixture
def folder() -> Iterator[Path]:
    path = home_folder()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def other_folder() -> Iterator[Path]:
    path = home_folder()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def grants_grants(workspace_id: uuid.UUID, task_id: uuid.UUID) -> tuple[list[FolderGrant], bool]:
    async with bound_scope(workspace_id) as s:
        return await device_grants.grants_for(s, task_id)


async def person(s: Any, name: str) -> Actor:
    """Another member of the bound workspace, with an email `add_member` can name."""
    actor, _ = await member(s, name)
    s.add(User(actor_id=actor.id, email=f"{name.lower()}{uuid.uuid4().hex[:8]}@example.com", name=name))
    await s.flush()
    return actor


def worktree(root: Path) -> tuple[Path, Path]:
    """A repository at `root/main` and a worktree of it at `root/wt` (`wt/.git` is a pointer)."""
    return worktree_at(root / "main", root / "wt")


def worktree_at(main: Path, wt: Path) -> tuple[Path, Path]:
    """A repository at `main` and a worktree of it at `wt`."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(main)], check=True)
    (main / "a.txt").write_text("a\n")
    subprocess.run([*git, "-C", str(main), "add", "a.txt"], check=True)
    subprocess.run([*git, "-C", str(main), "commit", "-qm", "a"], check=True)
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", "-b", "wt", str(wt)], check=True)
    return main, wt
