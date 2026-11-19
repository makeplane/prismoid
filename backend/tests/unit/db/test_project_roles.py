# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The role matrix, and the 404-vs-403 distinction that protects existence.

The authz *drill* (two real users, viewer reads but cannot create, removed member loses access) is
validated live from the CLI; this pins the pure logic underneath it, which is the part a refactor is
most likely to get subtly wrong.
"""

from __future__ import annotations

import uuid

from prismoid.db.models.project import Project, Role, at_least, refusal


def _project(owner: uuid.UUID) -> Project:
    return Project(id=uuid.uuid4(), name="Pricing", owner_actor_id=owner)


def test_the_owner_outranks_any_membership_row() -> None:
    """`owner_actor_id` is the column so ownership cannot drift from a table. Even a stale
    membership row saying `viewer` does not demote the owner."""
    owner = uuid.uuid4()
    project = _project(owner)
    assert project.role_of(owner, {owner: Role.VIEWER}) is Role.OWNER


def test_a_member_is_their_row_and_a_stranger_is_none() -> None:
    project = _project(uuid.uuid4())
    editor, stranger = uuid.uuid4(), uuid.uuid4()
    assert project.role_of(editor, {editor: Role.EDITOR}) is Role.EDITOR
    assert project.role_of(stranger, {}) is None, "a stranger gets None, never a default of viewer"


def test_the_rank_ladder() -> None:
    assert at_least(Role.OWNER, Role.EDITOR) and at_least(Role.OWNER, Role.VIEWER)
    assert at_least(Role.EDITOR, Role.VIEWER)
    assert not at_least(Role.VIEWER, Role.EDITOR), "a viewer cannot create"
    assert not at_least(Role.EDITOR, Role.OWNER), "an editor cannot change the budget"
    assert at_least(Role.VIEWER, Role.VIEWER), "the boundary is inclusive"


def test_a_viewer_reaching_for_work_hears_who_grants_it() -> None:
    """Every write a viewer tries (follow up, steer, pause, answer, replan, fork,
    typing) is one 403 that says what a viewer does and who grants more. Any other shortfall names
    the role it needs."""
    assert refusal(Role.VIEWER, Role.EDITOR) == "viewers watch; ask the Project owner for edit access"
    assert refusal(Role.EDITOR, Role.OWNER) == "this action needs the owner role; you are editor"
    assert refusal(Role.VIEWER, Role.OWNER) == "this action needs the owner role; you are viewer"
