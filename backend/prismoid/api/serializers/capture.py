# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Capture - request and response bodies."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.capture import Capture


class CaptureIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    url: str | None = Field(default=None, max_length=2000)
    project_id: UUID | None = None


class PromoteIn(BaseModel):
    goal: str = Field(default="", max_length=8000)  # blank ⇒ the capture's own text is the goal


class CaptureOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    text: str
    url: str | None
    status: str
    project_id: UUID | None
    promoted_task_id: UUID | None
    created_at: dt.datetime


def capture_out(c: Capture) -> CaptureOut:
    return CaptureOut(
        id=c.id,
        text=c.text,
        url=c.url,
        status=c.status,
        project_id=c.project_id,
        promoted_task_id=c.promoted_task_id,
        created_at=c.created_at,
    )
