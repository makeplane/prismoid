# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The project charter: owner-writes, everyone-reads, `""` clears. Instructions are the owner's
standing voice over every task filed in the project - the shared *ledger* is the editors' voice,
and the roles deliberately differ."""

from __future__ import annotations

import pytest

from prismoid.api.serializers.projects import project_out
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden
from prismoid.operations import projects as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_owner_sets_members_read_empty_string_clears() -> None:
    async with session_scope() as session:
        owner, _ = await member(session, "Own")
        editor, _ = await member(session, "Edit")
        await session.flush()
        project = (await ops.create(session, owner, name="charter-probe")).project
        session.add(
            ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id)
        )
        await session.flush()

        assert project.instructions == "", "a new project has no charter"

        await ops.update(session, owner, project.id, instructions="  Cite sources. UK English.  ")
        read = await ops.get(session, project.id, editor)  # the project, the caller's role, the owner
        seen = read.project
        assert seen.instructions == "Cite sources. UK English.", "trimmed, and every member reads it"
        assert project_out(seen, "editor", read.owner).instructions == seen.instructions, (
            "the API carries the charter"
        )

        with pytest.raises(Forbidden):
            await ops.update(session, editor, project.id, instructions="editors cannot re-charter")

        await ops.update(session, owner, project.id, instructions="")
        assert (await ops.get(session, project.id, owner)).project.instructions == "", (
            '"" IS the cleared charter'
        )
