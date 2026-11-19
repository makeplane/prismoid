# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The first run of Prismoid on one computer (ADR-077): the one owner, the catalog, the skills, the CLI's key.

`prismoid up` calls `first_run` once, when there is no User yet. It is an operation in all but folder:
it owns its transactions, and it touches the database only through the operations that already
own those tables.

Resumable by construction. The seeds (catalog, routing, built-in skills) are idempotent - each adds
only what is missing - and they run BEFORE the owner is made, so a first run that dies part-way
leaves nothing but seeds and the retry simply runs again. Only the owner is not idempotent; it is
made last but one, under a lock, and its existence is what closes the door.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.engine import get_engine
from prismoid.db.models.actor import Actor
from prismoid.db.models.user import User
from prismoid.errors import Conflict, ValidationFailed
from prismoid.operations import auth, models, routing, skills

MIN_PASSWORD_LENGTH = 12  # the signup rule (api/serializers/auth.py): length is the only rule that helps
_DEFAULT_MODELS = Path(__file__).resolve().parents[1] / "runner" / "default_models.yaml"
_LOCK_KEY = "prismoid:first_run"
# The login door's own rule (LoginIn.email is an EmailStr): an owner made with an address it
# refuses could never sign in to the web app - `x@prismoid.local` was accepted here, then every
# login 422'd on the reserved `.local` name.
_EMAIL = TypeAdapter(EmailStr)


class AlreadySetUp(Conflict):
    """409 `already_set_up` - Prismoid on this computer already has its owner."""

    code = "already_set_up"


async def needs_first_run(session: AsyncSession) -> bool:
    return await User.count(session) == 0


async def first_run(session: AsyncSession, *, email: str, name: str, password: str) -> str:
    """Make the one owner, seed what a fresh Prismoid needs, and return a raw API key for the CLI.

    The caller saves the key (`client.save_token`); it is shown here once and stored only as a hash.
    A second concurrent first run waits on the lock, then finds the owner and is refused."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(
            "password too short", fields={"password": f"at least {MIN_PASSWORD_LENGTH} characters"}
        )
    _check_email(email)
    # A dedicated connection holds the lock across the commits below (a pooled session would hand
    # its connection back at the first commit, and a transaction-scoped lock with it).
    async with get_engine().connect() as lock:
        await lock.execute(text("select pg_advisory_lock(hashtext(:k))"), {"k": _LOCK_KEY})
        try:
            if not await needs_first_run(session):
                raise AlreadySetUp("Prismoid is already set up on this computer")
            await _seed(session)
            result = await auth.signup(
                session, email=email, name=name, password=password, local_first_run=True
            )
            actor = await session.get(Actor, result.user.actor_id)
            if actor is None:  # signup just made it
                raise AlreadySetUp("the owner vanished mid-run")
            _, raw = await auth.create_api_key(session, actor, "prismoid cli")
            return raw
        finally:
            await lock.execute(text("select pg_advisory_unlock(hashtext(:k))"), {"k": _LOCK_KEY})


def _check_email(email: str) -> None:
    """Refuse, before anything is made, an address the web login would refuse. Validation only:
    signup keeps its own normalization, so the stored address is what it always was."""
    try:
        _EMAIL.validate_python(email)
    except ValidationError as exc:
        why = str(exc.errors()[0].get("msg", "not a valid email address"))
        why = why.removeprefix("value is not a valid email address: ")
        raise ValidationFailed(
            "the web app could not sign in with that email", fields={"email": why}
        ) from None


async def _seed(session: AsyncSession) -> None:
    data = yaml.safe_load(_DEFAULT_MODELS.read_text())
    data = data if isinstance(data, dict) else {}
    await models.seed_catalog(session, list(data.get("models") or []))
    await routing.seed_routing(session, data.get("routing") or {}, settings=data.get("settings") or {})
    await skills.seed_builtins(session)
