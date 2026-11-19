# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device helper's wire values round-trip through JSON."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import BaseModel

from prismoid.schemas.device import (
    ExecIn,
    ExecOut,
    FileIn,
    FolderGrant,
    FrameOut,
    FramePathsWire,
    FrameSpecIn,
    InterruptedOut,
)
from prismoid.schemas.machines import FrameKind

PATHS = FramePathsWire(
    task="/h/t",
    work="/h/w",
    artifacts="/h/a",
    skills="/h/s",
    verify="/h/v",
    inputs="/h/t/inputs",
    browser="/h/b",
    out="/h/o",
)

MODELS: list[BaseModel] = [
    FolderGrant(path="/Users/me/code", mode="rw"),
    FrameSpecIn(
        task_id="t1",
        kind=FrameKind.MACHINE,
        grants=[FolderGrant(path="/x", mode="ro")],
        docker=True,
        allow_domains=["example.com"],
        egress_version=2,
    ),
    PATHS,
    FrameOut(ref="device-t1", paths=PATHS, device_name="MacBook Pro"),
    ExecIn(exec_id="e1", command="ls", timeout=5.0, system=True),
    ExecOut(exit_code=0, stdout="a", stderr="", truncated=False),
    FileIn(path="/work/a", data_b64="YQ==", mode=0o600, owner="system"),
    InterruptedOut(exec_id="e1", command_head="ls", started_at=dt.datetime(2026, 10, 29, tzinfo=dt.UTC)),
]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: type(m).__name__)
def test_wire_roundtrip(model: BaseModel) -> None:
    assert type(model).model_validate_json(model.model_dump_json()) == model


def test_spec_defaults() -> None:
    spec = FrameSpecIn(task_id="t", kind=FrameKind.SEAT)
    assert (spec.grants, spec.docker, spec.allow_domains, spec.egress_version) == ([], False, [], 1)
    assert FileIn(path="p", data_b64="").mode == 0o644
