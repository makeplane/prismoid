# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the Docker proxy policy tests, shared by the
`test_docker_policy*` files: a granted root, and `docker_policy` run on the bodies Docker clients send.
A plain module, not a test file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from prismoid.device.docker_policy import (
    Verdict,
    vet_request,
)

REF, TASK = "device-t1", "t1"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "demo"
    (folder / "data").mkdir(parents=True)
    return Path(os.path.realpath(folder))


def vet(
    method: str,
    path: str,
    body: Any = None,
    *roots: Path,
    read_only: tuple[Path, ...] = (),
    protected: tuple[Path, ...] = (),
) -> Verdict:
    raw = b"" if body is None else json.dumps(body).encode()
    return vet_request(
        method,
        path,
        raw,
        ref=REF,
        task_id=TASK,
        roots=[str(r) for r in (*roots, *read_only)],
        read_only=[str(r) for r in read_only],
        protected=[str(p) for p in protected],
    )


def create(host: dict[str, Any], root: Path, *, protected: tuple[Path, ...] = (), **config: Any) -> Verdict:
    return vet(
        "POST",
        "/v1.47/containers/create?name=web",
        {"Image": "alpine", **config, "HostConfig": host},
        root,
        protected=protected,
    )


def refused(verdict: Verdict, *words: str) -> None:
    assert not verdict.allow, verdict
    for word in words:
        assert word in verdict.reason, verdict.reason


def inspected(host: dict[str, Any] | None = None, *mounts: dict[str, Any]) -> dict[str, Any]:
    """A container as the daemon's inspect shows it: its masked paths filled in, its shm its own."""
    defaults = {
        "MaskedPaths": ["/proc/acpi", "/proc/kcore", "/proc/keys"],
        "ReadonlyPaths": ["/proc/bus", "/proc/sys", "/proc/sysrq-trigger"],
        "ShmSize": 4195352576,  # OrbStack's default: the daemon's, never refused
        "NetworkMode": "bridge",
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "Devices": [],
    }
    return {"HostConfig": {**defaults, **(host or {})}, "Mounts": list(mounts)}
