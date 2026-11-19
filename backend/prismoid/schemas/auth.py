# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Auth vocabulary: what a signed-out page may learn about this deployment (ADR-086).

`AuthConfig` is the value behind `GET /v1/auth/config`. It describes the deployment, never an
account: whether signup is open, the human-check site key, the legal and support links. Pure
value; the operation fills it from settings, a schema never imports conf.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SignupMode(StrEnum):
    OPEN = "open"
    INVITE = "invite"


class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    signup: SignupMode
    terms_url: str | None = None
    privacy_url: str | None = None
    support_email: str | None = None
