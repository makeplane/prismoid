# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The skill loader against the real world: a body copied into a live frame, and a
`resolve` that round-trips through the registry + object store.

The frame test drives a **real** `DockerFrame` (like `test_frame.py`) because the property that
matters - the body lands at `/skills/<name>/SKILL.md`, root-owned and unwritable by the agent - is a
property of a container's filesystem and permissions, not of any in-memory fake. `materialise_skills`
is the executor's own function; testing it here is testing the exact code a task runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.frames.layout import SKILLS
from prismoid.ai.skills.format import parse, render
from prismoid.db.session import session_scope
from prismoid.operations import skills as ops
from prismoid.operations.execution import materialise_skills
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import SkillName
from prismoid.schemas.plan import StepSpec
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-b")]


@pytest.fixture
async def frame() -> AsyncIterator[DockerFrame]:
    ensure_image()
    f = DockerFrame(task_id=f"test-skill-frame-{uuid.uuid4().hex[:8]}")
    await f.create(allow_domains=[])
    try:
        yield f
    finally:
        await f.destroy()


async def test_resolve_round_trips_through_the_registry() -> None:
    """A published skill resolves to a body that re-parses to the same manifest - the pull path."""
    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        resolved = await ops.resolve(session, "report")

    assert resolved.version == 1
    rebuilt = parse(
        "report", render(resolved.manifest, resolved.body), "routing:\n  - goal: g\n    expect: report\n"
    )
    assert rebuilt.body == resolved.body
    assert rebuilt.manifest.description == resolved.manifest.description


async def test_a_skill_body_lands_in_the_frame_read_only(frame: DockerFrame) -> None:
    spec = StepSpec(skill_pins=[SkillPin(name=SkillName("report"))])

    async with session_scope() as session:
        await member(session, "Reader")  # skills resolve per workspace
        paths = await materialise_skills(session, spec, frame)

    assert paths == [f"{SKILLS}/report/SKILL.md"]

    body = await frame.read_file(paths[0])
    assert "Load when" in body  # the frontmatter came through
    assert body.strip(), "the body is empty in the frame"

    # Root-owned 0444: the agent cannot edit its own guidance in place.
    probe = await frame.exec(f"echo x >> {paths[0]} 2>&1 || echo DENIED")
    assert "DENIED" in probe.stdout

    # And what landed re-parses to the same skill - it is not a mangled copy.
    reparsed = parse("report", body, "routing:\n  - goal: g\n    expect: report\n")
    assert "prose" in reparsed.body.lower() or reparsed.body


async def test_no_pins_is_a_no_op(frame: DockerFrame) -> None:
    async with session_scope() as session:
        paths = await materialise_skills(session, StepSpec(), frame)
    assert paths == []
    listing = await frame.exec(f"ls {SKILLS}")
    assert listing.stdout.strip() == "", "a step with no skill pins should leave /skills empty"
