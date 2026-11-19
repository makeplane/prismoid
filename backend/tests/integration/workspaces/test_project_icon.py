# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The project's face: owner-sets, everyone-reads, clear flags return to the default. The icon
is presentation the server stores and never interprets - shapes are validated at the door
(ProjectUpdateIn's pattern), roles by the same owner gate every setting speaks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prismoid.api.serializers.projects import ProjectUpdateIn, project_list_out, project_out
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden
from prismoid.operations import projects as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_owner_sets_face_members_read_clears_return_default() -> None:
    async with session_scope() as session:
        owner, _ = await member(session, "Own")
        editor, _ = await member(session, "Edit")
        await session.flush()
        project = (await ops.create(session, owner, name="icon-probe")).project
        session.add(
            ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id)
        )
        await session.flush()

        assert project.icon is None and project.icon_hue is None, "a new project wears the default face"

        await ops.update(session, owner, project.id, icon="lucide:rocket", icon_hue=3)
        read = await ops.get(session, project.id, editor)  # the project, the caller's role, the owner
        seen = read.project
        assert (seen.icon, seen.icon_hue) == ("lucide:rocket", 3), "every member reads the face"
        assert project_out(seen, "editor", read.owner).icon == "lucide:rocket", "the detail shape carries it"
        assert project_list_out(seen, "editor").icon_hue == 3, "the LIST shape carries it too"

        with pytest.raises(Forbidden):
            await ops.update(session, editor, project.id, icon="emoji:🚀")

        # An emoji face; then hue alone returns to neutral; then the whole face clears.
        await ops.update(session, owner, project.id, icon="emoji:🚀")
        assert (await ops.get(session, project.id, owner)).project.icon == "emoji:🚀"
        await ops.update(session, owner, project.id, clear_hue=True)
        assert (await ops.get(session, project.id, owner)).project.icon_hue is None
        await ops.update(session, owner, project.id, clear_icon=True)
        refreshed = (await ops.get(session, project.id, owner)).project
        assert refreshed.icon is None and refreshed.icon_hue is None


def test_update_in_refuses_malformed_faces() -> None:
    # The door validates shape so the store never holds junk: bad prefix, bad slug charset,
    # out-of-ramp hue - each refused at the serializer.
    for bad in ("rocket", "lucide:", "lucide:Not A Slug", "svg:<script>"):
        with pytest.raises(ValidationError):
            ProjectUpdateIn(icon=bad)
    with pytest.raises(ValidationError):
        ProjectUpdateIn(icon_hue=8)
    assert ProjectUpdateIn(icon="lucide:rocket", icon_hue=7).icon == "lucide:rocket"
    assert ProjectUpdateIn(icon="emoji:🗂️").icon == "emoji:🗂️"
