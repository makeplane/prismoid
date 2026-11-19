# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid up`'s first run (ADR-077): the one owner, the seeds, the CLI's key, and no members.

The first-run tests need a database with no User in it, so they build their own: a clone of the
current one with the identity tables emptied, dropped afterwards. The shared dev DB is never touched."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import func, select, text

from prismoid import keyfile
from prismoid.conf import settings
from prismoid.db import engine as engine_module
from prismoid.db import session as session_module
from prismoid.db.models.actor import Actor, Scope
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.credential import Credential
from prismoid.db.models.model_catalog import ModelCatalogEntry
from prismoid.db.models.skill import SkillRecord
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.errors import FeatureDisabled
from prismoid.local import bootstrap
from prismoid.operations import project_invites, projects
from prismoid.operations.auth import _hasher  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"


def _root_and_name(url: str) -> tuple[str, str]:
    root, _, name = url.replace("+asyncpg", "").rpartition("/")
    return root, name


@pytest.fixture
async def empty_prismoid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    """A private clone of the current DB with every identity row gone; local mode on, objects on disk."""
    root, name = _root_and_name(settings.api.database_url)
    clone = f"{name}_c3_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(f"{root}/postgres")
    try:
        await engine_module.dispose_engine()  # a template can have no other connection, ours included
        for attempt in range(10):
            try:
                await admin.execute(f'create database "{clone}" template "{name}"')
                break
            except asyncpg.PostgresError:
                if attempt == 9:
                    raise
                if "_xt_" in name:  # this xdist worker's own clone: what still holds it is a dead test's
                    await admin.execute(
                        "select pg_terminate_backend(pid) from pg_stat_activity where datname = $1", name
                    )
                await asyncio.sleep(0.3 * (attempt + 1))
        conn = await asyncpg.connect(f"{root}/{clone}")
        try:
            await conn.execute("set session_replication_role = replica")
            await conn.execute("truncate actors cascade")
        finally:
            await conn.close()
        await engine_module.dispose_engine()
        engine_module.get_engine.cache_clear()
        session_module.get_sessionmaker.cache_clear()
        monkeypatch.setattr(
            settings.api, "database_url", f"{settings.api.database_url.rpartition('/')[0]}/{clone}"
        )
        monkeypatch.setattr(settings, "environment", "local")
        monkeypatch.setattr(settings.worker, "storage_backend", "fs")
        monkeypatch.setattr(settings.local, "home", tmp_path)
        monkeypatch.setattr(
            settings.auth, "signup_enabled", False
        )  # the door is shut; first run opens it alone
        keyfile.forget()
        yield
        await engine_module.dispose_engine()
        engine_module.get_engine.cache_clear()
        session_module.get_sessionmaker.cache_clear()
    finally:
        await admin.execute(f'drop database if exists "{clone}" with (force)')
        await admin.close()


async def test_first_run_creates_owner_workspace_catalog_skills(empty_prismoid: None) -> None:
    async with session_scope() as session:
        assert await bootstrap.needs_first_run(session)
        key = await bootstrap.first_run(session, email="Me@Example.com", name="Me", password=_PASSWORD)
    assert key.startswith("ck_")

    async with session_scope() as session:
        assert not await bootstrap.needs_first_run(session)
        user = await User.by_email(session, "me@example.com")
        assert user is not None
        credential = await session.get(Credential, user.actor_id)
        assert credential is not None and credential.password_hash.startswith("$argon2id$")
        assert _hasher.verify(credential.password_hash, _PASSWORD)
        assert await session.scalar(select(func.count()).select_from(ApiKey)) == 1
        assert (await session.scalar(select(func.count()).select_from(ModelCatalogEntry)) or 0) > 0
        assert (await session.scalar(select(func.count()).select_from(SkillRecord)) or 0) > 0
        owners = await session.scalar(
            text("select count(*) from workspace_members where role = 'owner' and actor_id = :a"),
            {"a": user.actor_id},
        )
        assert owners == 1
        owner = await session.get(Actor, user.actor_id)
        assert owner is not None and owner.has_scope(Scope.ADMIN)  # the person on the laptop runs it


async def test_first_run_twice_refuses(empty_prismoid: None) -> None:
    async with session_scope() as session:
        await bootstrap.first_run(session, email="me@example.com", name="Me", password=_PASSWORD)
    async with session_scope() as session:
        with pytest.raises(bootstrap.AlreadySetUp):
            await bootstrap.first_run(session, email="other@example.com", name="Other", password=_PASSWORD)
    async with session_scope() as session:
        assert await User.count(session) == 1


async def test_two_concurrent_first_runs_make_one_owner(empty_prismoid: None) -> None:
    async def attempt(email: str) -> object:
        async with session_scope() as session:
            return await bootstrap.first_run(session, email=email, name="N", password=_PASSWORD)

    results = await asyncio.gather(attempt("a@example.com"), attempt("b@example.com"), return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, bootstrap.AlreadySetUp) for r in results) == 1


async def test_short_password_refused_before_anything_is_made(empty_prismoid: None) -> None:
    async with session_scope() as session:
        with pytest.raises(Exception, match="too short"):
            await bootstrap.first_run(session, email="me@example.com", name="Me", password="short")
        assert await bootstrap.needs_first_run(session)


async def test_signup_gate_stays_shut_for_everyone_else(empty_prismoid: None) -> None:
    from prismoid.errors import SignupDisabled
    from prismoid.operations import auth

    async with session_scope() as session:
        with pytest.raises(SignupDisabled, match=r"^Signups are paused\.$") as refused:
            await auth.signup(session, email="x@example.com", name="X", password=_PASSWORD)
    assert refused.value.code == "signup_disabled"


async def test_local_first_run_flag_is_ignored_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.errors import SignupDisabled
    from prismoid.operations import auth

    monkeypatch.setattr(settings.auth, "signup_enabled", False)
    monkeypatch.setattr(settings, "environment", "test")
    async with session_scope() as session:
        with pytest.raises(SignupDisabled, match=r"^Signups are paused\.$") as refused:
            await auth.signup(
                session, email="x@example.com", name="X", password=_PASSWORD, local_first_run=True
            )
    assert refused.value.code == "signup_disabled"


async def test_add_member_refused_in_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "local")
    async with session_scope() as session:
        with pytest.raises(FeatureDisabled, match="local mode"):
            await projects.add_member(
                session, Actor(), uuid.uuid4(), email="a@example.com", role=projects.Role.EDITOR
            )


async def test_join_link_accept_refused_in_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "local")
    async with session_scope() as session:
        with pytest.raises(FeatureDisabled, match="local mode"):
            await project_invites.accept(session, Actor(), "si_whatever", None)
