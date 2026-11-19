# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A member removed from a project is told, in their inbox."""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import projects as project_ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_a_removed_member_is_told() -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        await s.commit()
        before = len(await InboxItem.list_for(s, bob))

        await project_ops.remove_member(s, ada, project.id, bob.id)

        after = await InboxItem.list_for(s, bob)
        assert len(after) == before + 1, (
            f"no signal for the removed member: inbox kinds={[i.kind for i in after]}"
        )
