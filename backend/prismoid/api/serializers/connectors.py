# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every connector shape, one file. `*In` = request, `*Out` = response.

Note what is absent: every token, secret, ciphertext column, state hash, and flow blob. A
connector's wire shape is its identity, its status, and its tool inventory - the credential
that makes it work is never serializable, in either direction after creation.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prismoid.db.models.connector import Connector
from prismoid.schemas.connector_catalog import CatalogAuth, CatalogEntry, vocabulary
from prismoid.schemas.connectors import (
    AuthKind,
    ConnectorStatus,
    ConnectorToolInfo,
    ToolPolicy,
    effective_policies,
)


class CatalogEntryOut(BaseModel):
    """One known provider from the data catalog. `keywords` ship so the composer's
    suggestion chip can run the same word-boundary grammar client-side, at zero latency."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str
    vendor: str
    description: str
    category: str
    server_url: str
    auth: CatalogAuth
    icon: str
    docs_url: str
    auth_note: str
    keywords: list[str]


class CatalogOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[CatalogEntryOut]


def catalog_entry_out(entry: CatalogEntry) -> CatalogEntryOut:
    return CatalogEntryOut(
        slug=entry.slug,
        name=entry.name,
        vendor=entry.vendor,
        description=entry.description,
        category=entry.category,
        server_url=entry.server_url,
        auth=entry.auth,
        icon=entry.icon or entry.slug,
        docs_url=entry.docs_url,
        auth_note=entry.auth_note,
        keywords=list(entry.keywords),
    )


class ConnectorCreateIn(BaseModel):
    server_url: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, max_length=80)
    # A pasted static token, for servers that hand keys out of a settings page. Accepted at
    # creation, encrypted at rest, never echoed back.
    bearer_token: str | None = Field(default=None, max_length=4000)
    # A pre-registered OAuth client, for servers without dynamic client registration.
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=500)


class AppSignInIn(BaseModel):
    """The phone app starting an OAuth sign-in it will finish itself (RFC 8252): the S256 hash of
    a secret only it holds, and which app build it is - the callback sends the browser back to
    that app's scheme, picked from a fixed list."""

    code_challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    return_scheme: Literal["prismoid", "prismoid-dev", "prismoid-staging"]


class AuthorizeIn(BaseModel):
    """Optional: the web sends no body."""

    app: AppSignInIn | None = None


class AppFinishIn(BaseModel):
    """The app back from the browser: the one-time code the callback handed it, and its secret."""

    return_code: str = Field(min_length=1, max_length=200)
    code_verifier: str = Field(pattern=r"^[A-Za-z0-9._~-]{43,128}$")


class ConnectorConnectIn(BaseModel):
    """One-click connect: a catalog entry by slug, or a custom server by URL -
    exactly one. Create + begin-authorization in one round trip."""

    catalog_slug: str | None = Field(default=None, max_length=48)
    server_url: str | None = Field(default=None, max_length=2000)
    name: str | None = Field(default=None, max_length=80)
    bearer_token: str | None = Field(default=None, max_length=4000)
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=500)
    app: AppSignInIn | None = None  # the phone app finishes the sign-in itself

    @model_validator(mode="after")
    def _exactly_one(self) -> ConnectorConnectIn:
        if (self.catalog_slug is None) == (self.server_url is None):
            raise ValueError("pass exactly one of catalog_slug or server_url")
        return self


class ConnectorUpdateIn(BaseModel):
    """Rename, or flip the intent-attach dial - at least one field."""

    name: str | None = Field(default=None, min_length=1, max_length=80)  # the slug (prefix) is fixed
    auto_attach: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> ConnectorUpdateIn:
        if self.name is None and self.auto_attach is None:
            raise ValueError("nothing to update - pass name and/or auto_attach")
        return self


class PoliciesIn(BaseModel):
    """The whole overrides map, replace semantics: what the permissions screen holds is what
    the row stores. Tool names are validated against the cached inventory in operations."""

    policies: dict[str, ToolPolicy] = Field(max_length=200)


class ConnectorToolOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    # MCP ToolAnnotations as probed (None = the server declared nothing), and the policy that
    # actually governs the tool right now - with where it came from, so the permissions screen
    # can show "default" vs an explicit override without re-deriving the dial client-side.
    read_only: bool | None = None
    destructive: bool | None = None
    policy: ToolPolicy = ToolPolicy.ASK
    policy_source: Literal["default", "override"] = "default"


class ConnectorOut(BaseModel):
    """A connector, as listed. The credential is exactly what is not here."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    slug: str
    server_url: str
    # The enums, not `str`: the OpenAPI schema carries the whole vocabulary, so the generated
    # web client gets a closed union and a new status is a compile error there, not a silent
    # fallback tone (the hand-synced-strings drift a review caught).
    status: ConnectorStatus
    auth_kind: AuthKind
    tools: list[ConnectorToolOut]
    instructions: str | None
    last_error: str | None
    auto_attach: bool
    catalog_slug: str | None
    # The row's suggestion vocabulary, computed by the ONE backend recipe (the "one
    # grammar" rule): the composer chip runs only the word-boundary match over these, never a
    # hand-mirrored copy of the stoplist + tokenizer.
    suggest_words: list[str]
    created_at: dt.datetime


class ConnectorConnectOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    connector: ConnectorOut
    # Present when consent is needed: where the owner's browser must go, right now (the flow
    # TTL is minutes). Null = nothing to authorize (live, bearer, or an error the row wears).
    authorize_url: str | None
    # True when the sign-in will come back to the app (`app` was sent). An older server never
    # says so - the app tells the two apart by this.
    app_sign_in: bool = False


class OAuthReturnIn(BaseModel):
    """The query an identity provider sends the browser back with. Anything else it adds is ignored."""

    state: str = Field(default="", max_length=200)
    code: str = Field(default="", max_length=4096)
    error: str = Field(default="", max_length=200)
    error_description: str = Field(default="", max_length=2000)


class AuthorizeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorize_url: str  # where the owner's browser must go; the callback does the rest
    app_sign_in: bool = False  # as on `ConnectorConnectOut`


def connector_out(row: Connector) -> ConnectorOut:
    infos = [ConnectorToolInfo.model_validate(t) for t in row.tools if t.get("name")]
    effective = effective_policies(row.tool_policies, infos)
    return ConnectorOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        server_url=row.server_url,
        status=ConnectorStatus(row.status),
        auth_kind=AuthKind(row.auth_kind),
        tools=[
            ConnectorToolOut(
                name=info.name,
                description=info.description,
                read_only=info.read_only,
                destructive=info.destructive,
                policy=effective[info.name],
                policy_source="override" if info.name in row.tool_policies else "default",
            )
            for info in infos
        ],
        instructions=row.instructions,
        last_error=row.last_error,
        auto_attach=row.auto_attach,
        catalog_slug=row.catalog_slug,
        suggest_words=sorted(
            vocabulary(
                slug=row.slug,
                name=row.name,
                catalog_slug=row.catalog_slug,
                tool_names=tuple(t.get("name", "") for t in row.tools if t.get("name")),
            )
        ),
        created_at=row.created_at,
    )
