# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A skill's name and bundled paths reach a Machine's shell or the host's disk nowhere: a row that
predates the format door's rules is refused at the mount and at export."""

from __future__ import annotations

import shlex
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotOrigin, BotRecord, BotStatus
from prismoid.db.models.skill import EvalStatus, SkillOrigin, SkillRecord, SkillStatus
from prismoid.errors import PolicyDenied
from prismoid.operations import plugins
from prismoid.operations.execution.mounts import materialise_skills
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import SkillName
from prismoid.schemas.plan import StepSpec
from tests.support.cast import member
from tests.support.fakes.fake_frames import FakeFrame

pytestmark = pytest.mark.integration

_HOSTILE = "x;touch pwned"


async def _legacy_row(db: AsyncSession, name: str, *, path: str) -> SkillRecord:
    """A published skill row as one written before names were checked: nothing parsed it."""
    sha = await storage.aput(b"Body.\n", bucket=settings.worker.skill_bucket)
    await member(db, "Legacy")
    row = SkillRecord(
        name=name,
        version=1,
        status=SkillStatus.PUBLISHED.value,
        origin=SkillOrigin.USER.value,
        eval_status=EvalStatus.GREEN.value,
        manifest={
            "name": name,
            "description": "Load when a test needs one. Not for anything else.",
            "body_sha256": sha,
            "cases": [],
            "files": [{"path": path, "sha256": sha, "size": 6}],
        },
        body_sha256=sha,
        body_key=sha,
        workspace_id=tenant.workspace_of(db),
    )
    db.add(row)
    await db.commit()
    return row


def _files(root: Path) -> set[Path]:
    """Every file written under `root`."""
    return {path for path in root.rglob("*") if path.is_file()}


async def _legacy_bot(db: AsyncSession, name: str, *, skill: str) -> Actor:
    """A published bot row pinning `skill`, as one written before pins were checked: nothing parsed it."""
    author, _ = await member(db, "Exporter")
    db.add(
        BotRecord(
            name=name,
            version=1,
            status=BotStatus.PUBLISHED.value,
            origin=BotOrigin.USER.value,
            owner_actor_id=author.id,
            manifest={
                "name": name,
                "description": "Reviews exported things. Load when: an export review is asked. "
                "Not for: drafting.",
                "skills": [skill],
                "cases": [{"goal": "Review it", "expect": name}],
            },
            instructions="Review.\n",
            eval_status=EvalStatus.GREEN.value,
            workspace_id=tenant.workspace_of(db),
        )
    )
    await db.commit()
    return author


async def test_mounting_a_legacy_row_with_a_hostile_name_runs_nothing(db: AsyncSession) -> None:
    await _legacy_row(db, _HOSTILE, path="scripts/run.py")
    frame = FakeFrame(f"skill-names-{uuid.uuid4().hex[:8]}")
    spec = StepSpec(skill_pins=[SkillPin(name=SkillName(_HOSTILE), version=1)])

    with pytest.raises(PolicyDenied, match="not a legal skill name"):
        await materialise_skills(db, spec, frame)

    assert frame.execs == []
    assert frame.files == {}


async def test_mounting_a_legacy_row_with_a_hostile_bundled_path_runs_nothing(db: AsyncSession) -> None:
    name = f"legacy{uuid.uuid4().hex[:8]}"
    await _legacy_row(db, name, path="scripts/../../etc/cron.d/x")
    frame = FakeFrame(f"skill-names-{uuid.uuid4().hex[:8]}")
    spec = StepSpec(skill_pins=[SkillPin(name=SkillName(name), version=1)])

    with pytest.raises(PolicyDenied, match="bundled file"):
        await materialise_skills(db, spec, frame)

    assert frame.execs == []
    assert frame.files == {}


@pytest.mark.parametrize(
    ("name", "path"), [("../../../escape", "scripts/run.py"), ("legacy", "scripts/../../../../../escape")]
)
async def test_exporting_a_bot_with_a_hostile_legacy_skill_writes_nothing_outside(
    db: AsyncSession, tmp_path: Path, name: str, path: str
) -> None:
    skill = f"{name}{uuid.uuid4().hex[:8]}"
    await _legacy_row(db, skill, path=path)
    bot = f"exporter-{uuid.uuid4().hex[:8]}"
    author = await _legacy_bot(db, bot, skill=skill)
    out = tmp_path / "a" / "b" / "out"

    bundle, notes = await plugins.export_bot(db, author, bot, out)

    assert [note for note in notes if skill in note and "left out" in note]
    assert not (bundle / "skills").exists()
    assert _files(tmp_path) == _files(bundle)


async def test_a_legal_skill_mounts_with_one_quoted_mkdir(db: AsyncSession) -> None:
    name = f"legit{uuid.uuid4().hex[:8]}"
    await _legacy_row(db, name, path="scripts/deep/run.py")
    frame = FakeFrame(f"skill-names-{uuid.uuid4().hex[:8]}")
    spec = StepSpec(skill_pins=[SkillPin(name=SkillName(name), version=1)])

    paths = await materialise_skills(db, spec, frame)

    assert paths == [f"/skills/{name}/SKILL.md", f"/skills/{name}/scripts/deep/run.py"]
    assert set(frame.files) == set(paths)
    assert [shlex.split(command) for command in frame.execs] == [
        ["mkdir", "-p", "--", f"/skills/{name}", f"/skills/{name}/scripts/deep"]
    ]
