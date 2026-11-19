# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Device grant shapes. `*In` = request, `*Out` = response."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prismoid.db.models.device_grant import DeviceGrant
from prismoid.schemas.device_grants import GrantKind, GrantMode


class DeviceGrantIn(BaseModel):
    task_id: uuid.UUID
    kind: GrantKind
    # A folder's absolute path on this computer (refused: `/`, the home, system folders, secrets).
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    mode: GrantMode = GrantMode.RW

    @model_validator(mode="after")
    def _a_folder_has_a_path_and_docker_none(self) -> DeviceGrantIn:
        if self.kind is GrantKind.FOLDER and self.path is None:
            raise ValueError("a folder grant needs its path")
        if self.kind is GrantKind.DOCKER and (self.path is not None or "mode" in self.model_fields_set):
            raise ValueError("a Docker grant takes no path or mode")
        return self


class DeviceGrantOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    task_id: uuid.UUID
    kind: GrantKind
    path: str | None
    mode: GrantMode | None
    name: str | None  # its virtual root in the task's frame: `/work`, `/grants/<name>`
    created_at: dt.datetime
    revoked_at: dt.datetime | None


def device_grant_out(row: DeviceGrant) -> DeviceGrantOut:
    return DeviceGrantOut(
        id=row.id,
        task_id=row.task_id,
        kind=row.kind,
        path=row.path,
        mode=row.mode,
        name=row.name,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )
