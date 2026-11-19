# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Presence shapes (ADR-062). `*In` = request; the route answers 204 with no body."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PresenceIn(BaseModel):
    """A composer's typing frame. `conn` is the stream's own id (the `?conn=` the client opened
    the task stream with) - validated here, so a malformed frame is a 422, never echoed."""

    conn: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    typing: bool
    mode: Literal["follow_up", "steer", "answer"]
