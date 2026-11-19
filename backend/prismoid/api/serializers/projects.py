# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Projects - request and response bodies. The API presents the owner as a **user**, never an
actor: "actor" is an internal attribution word and does not appear in an external body."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.operations.projects import Person


class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    budget_usd: Decimal | None = Field(default=None, gt=0)


class ProjectUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    budget_usd: Decimal | None = Field(default=None, gt=0)
    # PATCH needs to distinguish "unset the budget" from "leave it": a bare null cannot. The client
    # sends `clear_budget: true` to make a project unlimited.
    clear_budget: bool = False
    # The charter - standing instructions for every task filed in the project. A string can express
    # its own clearing (""), so no flag.
    instructions: str | None = Field(default=None, max_length=4000)
    # The project's face. Self-describing: "lucide:<slug>" (a glyph the web renders) or
    # "emoji:<char>". Same PATCH grammar as the budget: `clear_icon` returns the project to the
    # default folder (and neutral hue); `clear_hue` alone returns the hue to neutral.
    icon: str | None = Field(default=None, pattern=r"^(lucide:[a-z0-9-]{1,60}|emoji:.{1,16})$")
    icon_hue: int | None = Field(default=None, ge=0, le=7)
    clear_icon: bool = False
    clear_hue: bool = False


class ProjectListOut(BaseModel):
    """A list row. The charter (`instructions`, ≤4000 chars) rides only the detail shape -
    a 200-row page of projects must not carry up to 800 KB of text no list view renders."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    owner_id: UUID  # the owner's USER id, presented as a user
    budget_usd: Decimal | None
    icon: str | None  # "lucide:<slug>" | "emoji:<char>" | null = default folder
    icon_hue: int | None  # hue-ramp index 0..7 | null = neutral
    role: str  # the requesting user's role in this project - viewer | editor | owner
    created_at: dt.datetime


class ProjectOwnerOut(BaseModel):
    """The owner, named. The owner is a column, never a member row, so the members
    list cannot name them - and every member is owed the name of who owns the Project."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID  # the owner's user id, the same value as `owner_id`
    name: str  # "" when the owner has no profile row to name
    email: (
        str | None
    )  # null for a workspace guest, who knows people by name only (`WorkspaceRole.sees_emails`)


class ProjectOut(ProjectListOut):
    instructions: str
    owner: ProjectOwnerOut  # the detail read only: a 200-row list must not pay a name lookup per page


def project_list_out(project: Project, role: str) -> ProjectListOut:
    return ProjectListOut(
        id=project.id,
        name=project.name,
        owner_id=project.owner_actor_id,
        budget_usd=project.budget_usd,
        icon=project.icon,
        icon_hue=project.icon_hue,
        role=role,
        created_at=project.created_at,
    )


def project_out(project: Project, role: str, owner: Person) -> ProjectOut:
    """`owner` has no default: every Project read names its owner, as the caller may see them."""
    return ProjectOut(
        id=project.id,
        name=project.name,
        owner_id=project.owner_actor_id,
        budget_usd=project.budget_usd,
        icon=project.icon,
        icon_hue=project.icon_hue,
        instructions=project.instructions,
        owner=ProjectOwnerOut(user_id=project.owner_actor_id, name=owner.name, email=owner.email),
        role=role,
        created_at=project.created_at,
    )


class MemberIn(BaseModel):
    email: str = Field(min_length=3, description="the user to share with, by email (§6)")
    role: str = Field(default="viewer", pattern="^(viewer|editor)$")


class RoleIn(BaseModel):
    role: str = Field(pattern="^(viewer|editor)$")


class TransferIn(BaseModel):
    email: str = Field(min_length=3, description="the user to transfer ownership to, by email (§6)")


class MemberOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: UUID  # the member's user id
    email: (
        str | None
    )  # null for a workspace guest, who knows people by name only (`WorkspaceRole.sees_emails`)
    name: str  # the member as a person - an opaque id in a member list is anti-collaborative
    role: str


def member_out(member: ProjectMember, person: Person) -> MemberOut:
    return MemberOut(actor_id=member.actor_id, email=person.email, name=person.name, role=member.role)


class ProjectUsageOut(BaseModel):
    """A project's month-to-date spend against its budget. Money as strings: a Decimal must not
    round-trip through a float, and the client formats currency, never arithmetics it."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    month_to_date_usd: str
    budget_usd: str | None  # null = unlimited
    remaining_usd: str | None  # null when the budget is unlimited


class RemovedOut(BaseModel):
    """A member revoked from a project - the typed answer to a DELETE (no bare dict)."""

    model_config = ConfigDict(frozen=True)

    removed: str  # the actor/user id whose grant is gone
