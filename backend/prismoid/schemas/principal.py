# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Principal - who a bearer credential resolved to, and the workspace it acts in (ADR-061):
the actor, the bound workspace and the caller's role there, plus the id of the credential
row itself - the login session's when it was a `cs_` session (the workspace switch writes to that
row), the API key's when it was a `ck_` key.

`workspace_id`/`role` are None when the person has no active membership left - every door but the way
back in (`deps.OrglessActorDep` and its session-only twin) answers 403.
Generic over the actor type because schemas sit below db (the layering law): the resolvers
return `Principal[Actor]`, and nothing here imports a row.

`Credential` is that row's id and table without the rest: what a screen ticket names,
so the screen socket's proof can re-read the row by id - never the token."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum


class CredKind(StrEnum):
    """Which table a credential row lives in: a login session's or an API key's."""

    SESSION = "session"
    API_KEY = "api_key"


@dataclass(frozen=True)
class Principal[A]:
    actor: A
    workspace_id: uuid.UUID | None
    role: str | None
    auth_session_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Credential:
    """The credential row a request authenticated with: `auth_sessions.id` or `api_keys.id`."""

    id: uuid.UUID
    kind: CredKind
