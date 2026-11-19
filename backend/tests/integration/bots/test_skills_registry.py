# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The seeded registry - the read side, against the real database.

These tests are deliberately **non-destructive**: the twelve builtins are seeded once at migrate and
never mutated in place, so a test that deleted them would be corrupting shared dev state, not
exercising it. Instead they assert the invariants the seed establishes - twelve published heads, each
a `builtin` at v1 with a body in MinIO - and that a second `seed_builtins` is a no-op. Idempotency is
the property that makes "seed on every migrate" safe, so it is the property worth a test.

Every ORM attribute is read *inside* the `session_scope()` block: `session_scope` commits on exit,
which expires the instances, and touching one after that raises `DetachedInstanceError`. So each test
materialises the plain values it needs before leaving the block, and asserts on those.
"""

from __future__ import annotations

import pytest

from prismoid import storage
from prismoid.conf import settings
from prismoid.db.models.skill import EvalStatus, SkillOrigin, SkillStatus
from prismoid.db.session import session_scope
from prismoid.operations import skills as ops
from prismoid.operations.skills import BUILTIN_DIR
from tests.support.cast import member

pytestmark = pytest.mark.integration

# The twelve builtin names, read from the source dirs - so this stays true if the set ever changes.
_BUILTIN_NAMES = sorted(p.name for p in BUILTIN_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


async def test_twelve_builtins_are_published_heads() -> None:
    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        heads = await ops.list_skills(session)
        rows = [(h.name, h.status, h.origin, h.version, h.eval_status) for h in heads]

    names = [r[0] for r in rows]
    assert names == sorted(set(names)), "a name appears twice - heads must be one row per skill"

    # The twelve builtins are all present, published, v1, green. Users can *also* publish skills,
    # so this is a subset check, not an equality - asserting "exactly 12" would break the
    # moment anyone publishes their own, which is the whole point of the registry.
    builtins = {name: (status, origin, version, evals) for name, status, origin, version, evals in rows}
    for name in _BUILTIN_NAMES:
        assert name in builtins, f"builtin {name} is not a published head"
        status, origin, version, evals = builtins[name]
        assert status == SkillStatus.PUBLISHED.value, name
        assert origin == SkillOrigin.BUILTIN.value, name
        assert version == 1, name
        assert evals == EvalStatus.GREEN.value, name


async def test_builtin_bodies_are_in_minio_by_hash() -> None:
    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        heads = await ops.list_skills(session)
        hashes = [(h.name, h.body_sha256) for h in heads]

    for name, sha in hashes:
        assert sha, f"{name} has no body hash"
        body = storage.get(sha, bucket=settings.worker.skill_bucket)
        assert storage.sha256(body) == sha, f"{name} body does not match its hash"


async def test_seed_is_idempotent() -> None:
    async with session_scope() as session:
        n = await ops.seed_builtins(session)
    assert n == 0, "builtins are already seeded - a re-seed must add nothing (§11)"


async def test_versions_of_a_builtin() -> None:
    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        versions = await ops.versions(session, "report")
        rows = [(v.version, v.status) for v in versions]

    assert len(rows) >= 1
    assert rows[0][0] == max(v for v, _ in rows), "versions are newest-first"
    assert any(v == 1 and s == SkillStatus.PUBLISHED.value for v, s in rows)
