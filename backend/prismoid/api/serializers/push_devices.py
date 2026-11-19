# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every push device shape, one file. `*In` = request, `*Out` = response (ADR-100)."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from prismoid.db.models.push_device import PushDevice
from prismoid.schemas.push import PushDevicePlatform, PushDeviceRegistration

TOKEN_MAX_BYTES = 1024


class PushDeviceIn(BaseModel):
    """A phone registering for push: its FCM registration token and what app build holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # FCM's tokens run a few hundred characters; the cap keeps a longer one inside the unique index's
    # btree row limit, a 422 here instead of a 500 from Postgres. The index counts bytes, so the
    # validator does too: 1024 multibyte characters would pass a character cap and still overflow.
    token: str = Field(min_length=1, max_length=TOKEN_MAX_BYTES)
    platform: PushDevicePlatform
    app_id: str = Field(min_length=1, max_length=200)  # the bundle id (iOS) or package name (Android)
    app_version: str = Field(min_length=1, max_length=64)

    @field_validator("token")
    @classmethod
    def _token_fits_the_index(cls, token: str) -> str:
        if len(token.encode()) > TOKEN_MAX_BYTES:
            raise ValueError(f"token is longer than {TOKEN_MAX_BYTES} bytes")
        return token

    def registration(self) -> PushDeviceRegistration:
        return PushDeviceRegistration(
            token=self.token, platform=self.platform, app_id=self.app_id, app_version=self.app_version
        )


class PushDeviceOut(BaseModel):
    """A registered phone. The token never leaves the server again."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    platform: PushDevicePlatform
    app_id: str
    app_version: str
    created_at: dt.datetime
    last_seen_at: dt.datetime


def device_out(row: PushDevice) -> PushDeviceOut:
    return PushDeviceOut(
        id=row.id,
        platform=row.platform,
        app_id=row.app_id,
        app_version=row.app_version,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
    )
