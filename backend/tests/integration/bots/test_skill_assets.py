# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skills carry assets, end to end (ADR-015): push with files → per-file CAS + manifest
pointers → resolve returns bytes → materialise into a REAL frame beside SKILL.md, read-only.

Needs the stack (`just up`) and Docker.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.db.models.actor import Actor
from prismoid.db.session import session_scope
from prismoid.operations import skills as ops
from prismoid.operations.execution.mounts import materialise_skills
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import AgentName, SkillName
from prismoid.schemas.plan import StepKind, StepSpec

# frame-spinning suite: ride a serialization lane like every sibling that boots Docker
# frames - unmarked, these escaped the justfile's three-lane loadgroup.
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-c")]

SKILL_MD = (
    "---\n"
    "description: >-\n"
    "  Load when a test needs a skill with bundled files. Not for anything else.\n"
    "---\n"
    "Read `references/shape.md` before you shape anything; run `scripts/check.py` before publishing.\n"
)
CASES = 'routing:\n  - goal: "bundled-files test skill"\n    expect: %s\n'
FILES = [
    ("references/shape.md", b"# The shape\nRows have ids.\n"),
    ("scripts/check.py", b"print('ok')\n"),
]


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


async def test_create_skill_endpoint_forwards_files(db: AsyncSession, actor: Actor) -> None:
    """POST /v1/skills with bundled files must store them (the endpoint used to drop `files`
    silently - only PUT forwarded them, and the CLI happened to always use PUT)."""
    import base64

    from prismoid.api.endpoints.skills import create_skill
    from prismoid.api.serializers.skills import SkillCreateIn, SkillFileIn

    name = f"postfiles{uuid.uuid4().hex[:8]}"
    body = SkillCreateIn(
        name=SkillName(name),
        skill_md=SKILL_MD,
        cases_yaml=CASES % name,
        files=[SkillFileIn(path=p, content_b64=base64.b64encode(data).decode()) for p, data in FILES],
    )
    out = await create_skill(body, actor, db)
    resolved = await ops.resolve(db, name, version=out.version)
    assert {(f.path, f.data) for f in resolved.files} == set(FILES)


async def test_files_round_trip_push_resolve_mount(db: AsyncSession, actor: Actor) -> None:
    name = f"bundled{uuid.uuid4().hex[:8]}"
    record = await ops.push(
        db, actor_id=actor.id, name=name, skill_md=SKILL_MD, cases_yaml=CASES % name, files=FILES
    )
    assert [f["path"] for f in record.manifest["files"]] == [p for p, _ in FILES]

    resolved = await ops.resolve(db, name, version=record.version)
    assert {(f.path, f.data) for f in resolved.files} == set(FILES)

    ensure_image()
    frame = DockerFrame(task_id=f"skill-assets-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        spec = StepSpec(
            kind=StepKind.STEP,
            agent=AgentName("writer"),
            title="t",
            skill_pins=[SkillPin(name=SkillName(name), version=record.version)],
        )
        paths = await materialise_skills(db, spec, frame)
        assert f"/skills/{name}/references/shape.md" in paths
        assert f"/skills/{name}/scripts/check.py" in paths

        shown = await frame.exec(f"cat /skills/{name}/references/shape.md")
        assert "Rows have ids" in shown.stdout
        ran = await frame.exec(f"/usr/local/bin/python3 /skills/{name}/scripts/check.py")
        assert ran.ok and "ok" in ran.stdout, "a bundled script must be runnable read-only"
        denied = await frame.exec(f"sh -c 'echo x >> /skills/{name}/scripts/check.py'")
        assert not denied.ok, "bundled files are guidance, not the agent's workshop - read-only"
    finally:
        await frame.destroy()
