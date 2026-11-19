# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every auth shape, one file. `*In` = request, `*Out` = response.

**The boundary vocabulary is enforced here**: `/v1/auth/me` returns a **UserOut** -
id, email, name, timezone, preferences - never an "ActorOut". The word *actor* appears in an
external payload in exactly one place in this product: the `actor: {type, id, name}` envelope
inside events and audit records. `just audit` greps this directory for `ActorOut` and fails on it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from prismoid.api.serializers.workspaces import WorkspaceBriefOut, workspace_brief_out
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace
from prismoid.schemas.auth import AuthConfig, SignupMode


class SignupIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=200)  # length is the only rule that helps
    invite_code: str | None = Field(default=None, max_length=200)  # an emailed oi_ code


class AuthConfigOut(BaseModel):
    """`GET /v1/auth/config`: the deployment, never an account (ADR-086)."""

    signup: SignupMode
    terms_url: str | None
    privacy_url: str | None
    support_email: str | None


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    """The emailed `cr_…` code is the whole authentication for this request."""

    code: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)  # the same rule signup enforces


class VerifyEmailIn(BaseModel):
    """The emailed `ev_…` code, read from the link's fragment by the page and posted here."""

    code: str = Field(min_length=20, max_length=200)


class SessionWorkspaceIn(BaseModel):
    """Switch the login session's active workspace - every tab on it follows."""

    workspace_id: UUID


class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class UserUpdateIn(BaseModel):
    """The editable slice of the profile, all optional - a PATCH sends what changed. Email is
    absent deliberately: changing the login identifier is a verification flow, not a PATCH."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _known_iana_zone(cls, v: str | None) -> str | None:
        """An unknown zone stored now is a formatting crash later, somewhere far from here."""
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str
    timezone: str
    preferences: dict[str, Any]


class MeOut(UserOut):
    """`/v1/auth/me` only - the caller's own profile plus their own admin standing, so the web
    can gate the Admin nav entry instead of 403-probing. Deliberately NOT on the general
    `UserOut`: whether someone else holds the keys is the operator's business, not a peer's.
    `workspace` is the workspace this credential is working in, with the caller's role there
    (ADR-061) - null once every membership is gone, when the person may only list, create or
    join workspaces."""

    is_admin: bool
    workspace: WorkspaceBriefOut | None
    can_create_workspace: bool  # false at the founding cap - the web hides "Create workspace"
    email_verified: bool  # the address is proved; the web offers a resend until it is


class SessionOut(BaseModel):
    """The plaintext session token. It exists in this response and nowhere else on earth."""

    token: str
    expires_at: dt.datetime
    user: UserOut


class ApiKeyOut(BaseModel):
    """A key, as listed. Note what is absent: the key."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    workspace_id: UUID  # the workspace the key acts in - it never follows a session's switch
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    """Creation, and only creation, carries the plaintext - once."""

    key: str


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.actor_id,  # the User's id IS the Actor's id. One spine.
        email=user.email,
        name=user.name,
        timezone=user.timezone,
        preferences=user.preferences,
    )


def me_out(
    user: User, workspace: Workspace | None, role: str | None, can_create_workspace: bool, *, is_admin: bool
) -> MeOut:
    brief = workspace_brief_out(workspace, role) if workspace is not None and role is not None else None
    return MeOut(
        **user_out(user).model_dump(),
        is_admin=is_admin,
        workspace=brief,
        can_create_workspace=can_create_workspace,
        email_verified=user.email_verified_at is not None,
    )


def api_key_out(key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut.model_validate(key)


def api_key_created_out(key: ApiKey, plaintext: str) -> ApiKeyCreatedOut:
    return ApiKeyCreatedOut(
        id=key.id,
        name=key.name,
        workspace_id=key.workspace_id,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        key=plaintext,
    )


def auth_config_out(config: AuthConfig) -> AuthConfigOut:
    return AuthConfigOut(**config.model_dump())
