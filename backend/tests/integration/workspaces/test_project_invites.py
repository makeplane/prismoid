# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project join links (ADR-062) and the PROJECT_SHARED knock - through HTTP.

The owner mints a bearer `si_` code, shown once; one person in the Project's workspace who holds it joins
at the link's role, and a link never lowers a role. A link is single-use and time-bound: the accept
that grants membership spends it, and after that it is dead for
everyone, the person it let in included. Every dead code - unknown, expired, revoked, used,
another workspace's - is one indistinguishable 404. The owner redeeming their own link
consumes nothing and never becomes a member row, and a redeemer whose login
session sits in another workspace follows the link into the invite's workspace: membership, knock and
active workspace all land there.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterable
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, update
from starlette.routing import BaseRoute

from prismoid.api import app as app_module
from prismoid.api.app import create_app
from prismoid.api.endpoints import project_invites as project_invite_views
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor, ActorStatus
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.project_invite import ProjectInvite
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceRole, WorkspaceStatus
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.schemas.ids import PROJECT_INVITE_PREFIX

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"
_DEAD = "this invite is invalid or has expired"
_RACE_TIMEOUT_S = 10  # a race that deadlocks must fail the test, never hang the suite


@pytest.fixture
async def client(silent_dispatcher: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    await auth_ops.flush_outbound_email()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Person:
    def __init__(self, token: str, me: dict, email: str) -> None:  # pyright: ignore[reportMissingTypeArgument]
        self.token, self.id, self.email = token, me["id"], email
        self.personal_workspace = me["workspace"]["id"]

    @property
    def h(self) -> dict[str, str]:
        return _h(self.token)


async def _signup(
    client: AsyncClient, name: str, *, invite_code: str | None = None, email: str | None = None
) -> Person:
    email = email or f"links{uuid.uuid4().hex[:12]}@example.com"
    body: dict[str, object] = {"email": email, "name": name, "password": _PASSWORD}
    if invite_code:
        body["invite_code"] = invite_code
    r = await client.post("/v1/auth/signup", json=body)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    me = await client.get("/v1/auth/me", headers=_h(token))
    return Person(token, me.json(), email)


async def _team(client: AsyncClient, owner: Person, name: str = "Acme") -> str:
    r = await client.post("/v1/workspaces", headers=owner.h, json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _join(workspace_id: str, person: Person, role: WorkspaceRole) -> None:
    async with session_scope() as s:
        await WorkspaceMember.add(s, uuid.UUID(workspace_id), uuid.UUID(person.id), role)
        await s.commit()


# ── this file's helpers ─────────────────────────────────────────────────────────────────────


async def _switch(client: AsyncClient, person: Person, workspace_id: str) -> None:
    r = await client.put("/v1/auth/session/workspace", headers=person.h, json={"workspace_id": workspace_id})
    assert r.status_code == 200, r.text


async def _active_workspace(client: AsyncClient, person: Person) -> str:
    r = await client.get("/v1/auth/me", headers=person.h)
    assert r.status_code == 200, r.text
    return str(r.json()["workspace"]["id"])


async def _project(
    client: AsyncClient, owner: Person, workspace_name: str, project_name: str
) -> tuple[str, str]:
    """A team workspace the owner founds and switches into, and a Project in it."""
    workspace = await _team(client, owner, workspace_name)
    await _switch(client, owner, workspace)
    r = await client.post("/v1/projects", headers=owner.h, json={"name": project_name})
    assert r.status_code == 201, r.text
    return workspace, str(r.json()["id"])


async def _launch(client: AsyncClient) -> tuple[Person, str, str]:
    """Ada, her team workspace Acme (her session in it), and her Project Launch."""
    ada = await _signup(client, "Ada")
    acme, project_id = await _project(client, ada, "Acme", "Launch")
    return ada, acme, project_id


async def _mint(client: AsyncClient, owner: Person, project_id: str, **body: Any) -> dict[str, Any]:
    r = await client.post(
        f"/v1/projects/{project_id}/invites", headers=owner.h, json={"role": "editor", **body}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _accept(client: AsyncClient, person: Person, code: str) -> Response:
    return await client.post("/v1/projects/invites/accept", headers=person.h, json={"code": code})


async def _listed(client: AsyncClient, owner: Person, project_id: str, invite_id: str) -> dict[str, Any]:
    r = await client.get(f"/v1/projects/{project_id}/invites", headers=owner.h)
    assert r.status_code == 200, r.text
    return next(i for i in r.json() if i["id"] == invite_id)


async def _roles(client: AsyncClient, viewer: Person, project_id: str) -> dict[str, str]:
    r = await client.get(f"/v1/projects/{project_id}/members", headers=viewer.h)
    assert r.status_code == 200, r.text
    return {m["actor_id"]: m["role"] for m in r.json()}


async def _share(client: AsyncClient, owner: Person, project_id: str, person: Person, role: str) -> None:
    r = await client.post(
        f"/v1/projects/{project_id}/members", headers=owner.h, json={"email": person.email, "role": role}
    )
    assert r.status_code == 201, r.text


async def _together(*calls: Coroutine[Any, Any, Response]) -> list[Response]:
    """Send the requests at once, under a bounded wait; nothing outlives the test."""
    tasks = [asyncio.ensure_future(c) for c in calls]
    try:
        return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=_RACE_TIMEOUT_S))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _knocks_in_db(person: Person) -> int:
    async with session_scope() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(InboxItem)
                .where(
                    InboxItem.actor_id == uuid.UUID(person.id),
                    InboxItem.kind == InboxKind.PROJECT_SHARED.value,
                )
            )
            or 0
        )


async def _shared_items(client: AsyncClient, person: Person) -> list[dict[str, Any]]:
    r = await client.get("/v1/inbox", headers=person.h)
    assert r.status_code == 200, r.text
    return [i for i in r.json()["items"] if i["kind"] == InboxKind.PROJECT_SHARED.value]


# ── the flows ───────────────────────────────────────────────────────────────────────────────


async def test_the_owner_mints_an_editor_link_and_a_member_joins_through_it(client: AsyncClient) -> None:
    ada, acme, project_id = await _launch(client)
    minted = await _mint(client, ada, project_id)
    code = minted["code"]
    assert code.startswith(PROJECT_INVITE_PREFIX)
    assert (minted["role"], minted["uses"], minted["state"]) == ("editor", 0, "live")
    assert "max_uses" not in minted  # every link is single-use: there is no count to report
    ttl = dt.datetime.fromisoformat(minted["expires_at"]) - utcnow()
    assert dt.timedelta(days=6, hours=23) < ttl <= dt.timedelta(days=7)  # PRISMOID_PROJECT_INVITE_TTL_DAYS
    listed = await client.get(f"/v1/projects/{project_id}/invites", headers=ada.h)
    assert code not in listed.text and "code" not in listed.json()[0]  # shown once, never again

    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    r = await _accept(client, bob, code)
    assert r.status_code == 200, r.text
    assert r.json() == {"project_id": project_id, "workspace_id": acme, "role": "editor"}

    assert (await _roles(client, bob, project_id))[bob.id] == "editor"
    listed_row = await _listed(client, ada, project_id, minted["id"])
    assert (listed_row["uses"], listed_row["state"]) == (1, "used_up")
    (knock,) = await _shared_items(client, bob)
    assert "Launch" in knock["title"] and knock["project_id"] == project_id
    assert knock["title"] == "Ada added you to Launch"


async def test_a_non_owner_cannot_mint_list_or_revoke(client: AsyncClient) -> None:
    ada, acme, project_id = await _launch(client)
    minted = await _mint(client, ada, project_id)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    assert (await _accept(client, bob, minted["code"])).status_code == 200  # an editor, in Acme now

    base = f"/v1/projects/{project_id}/invites"
    for method, path, body in (
        ("POST", base, {"role": "viewer"}),
        ("GET", base, None),
        ("DELETE", f"{base}/{minted['id']}", None),
    ):
        r = await client.request(method, path, headers=bob.h, json=body)
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"
    assert (await _listed(client, ada, project_id, minted["id"]))["state"] == "used_up"  # not revoked


async def test_every_dead_code_answers_the_same_404(client: AsyncClient) -> None:
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    cy = await _signup(client, "Cy")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _join(acme, cy, WorkspaceRole.MEMBER)

    expired = await _mint(client, ada, project_id)
    async with session_scope(workspace_id=uuid.UUID(acme)) as s:
        await s.execute(
            update(ProjectInvite)
            .where(ProjectInvite.id == uuid.UUID(expired["id"]))
            .values(expires_at=utcnow() - dt.timedelta(seconds=1))
        )
        await s.commit()
    assert (await _listed(client, ada, project_id, expired["id"]))["state"] == "expired"

    revoked = await _mint(client, ada, project_id)
    gone = await client.delete(f"/v1/projects/{project_id}/invites/{revoked['id']}", headers=ada.h)
    assert gone.status_code == 204, gone.text
    assert (await _listed(client, ada, project_id, revoked["id"]))["state"] == "revoked"

    used = await _mint(client, ada, project_id)
    assert (await _accept(client, cy, used["code"])).status_code == 200
    assert (await _listed(client, ada, project_id, used["id"]))["state"] == "used_up"

    zed = await _signup(client, "Zed")
    _, zeta_project = await _project(client, zed, "Zeta", "Elsewhere")
    foreign = await _mint(client, zed, zeta_project)

    dead = {
        "unknown": f"{PROJECT_INVITE_PREFIX}{uuid.uuid4().hex}",
        "expired": expired["code"],
        "revoked": revoked["code"],
        "used up": used["code"],
        "another workspace": foreign["code"],
    }
    answers: dict[str, tuple[int, dict[str, Any]]] = {}
    for label, code in dead.items():
        r = await _accept(client, bob, code)
        error = {k: v for k, v in r.json()["error"].items() if k != "request_id"}
        answers[label] = (r.status_code, error)
    assert all(a == (404, {"code": "not_found", "message": _DEAD}) for a in answers.values()), answers

    # Nothing moved: Bob is in neither Project, no use was spent, and his session stayed home.
    assert bob.id not in await _roles(client, ada, project_id)
    assert bob.id not in await _roles(client, zed, zeta_project)
    assert (await _listed(client, zed, zeta_project, foreign["id"]))["uses"] == 0
    assert await _active_workspace(client, bob) == bob.personal_workspace

    # A suspended workspace's link is dead too - the same words (switch_workspace's and join_by_invite's law).
    live = await _mint(client, ada, project_id)
    async with session_scope() as s:
        await s.execute(
            update(Workspace)
            .where(Workspace.id == uuid.UUID(acme))
            .values(status=WorkspaceStatus.SUSPENDED.value)
        )
        await s.commit()
    r = await _accept(client, bob, live["code"])
    assert (r.status_code, r.json()["error"]["message"]) == (404, _DEAD), r.text
    async with session_scope(workspace_id=uuid.UUID(acme)) as s:
        row = await s.get(ProjectInvite, uuid.UUID(live["id"]))
        assert row is not None and row.uses == 0
        assert await ProjectMember.get(s, uuid.UUID(project_id), uuid.UUID(bob.id)) is None


async def test_a_link_never_lowers_and_does_upgrade(client: AsyncClient) -> None:
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    cy = await _signup(client, "Cy")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _join(acme, cy, WorkspaceRole.MEMBER)
    await _share(client, ada, project_id, bob, "editor")
    await _share(client, ada, project_id, cy, "viewer")

    viewer_link = await _mint(client, ada, project_id, role="viewer")
    r = await _accept(client, bob, viewer_link["code"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "editor"
    assert (await _roles(client, ada, project_id))[bob.id] == "editor"
    unspent = await _listed(client, ada, project_id, viewer_link["id"])
    assert (unspent["uses"], unspent["state"]) == (0, "live")  # Bob accepted nothing new

    editor_link = await _mint(client, ada, project_id, role="editor")
    r = await _accept(client, cy, editor_link["code"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "editor"
    assert (await _roles(client, ada, project_id))[cy.id] == "editor"
    assert (await _listed(client, ada, project_id, editor_link["id"]))["uses"] == 1


async def test_the_owner_redeeming_their_own_link(client: AsyncClient) -> None:
    """The owner is the column, never a member row - redeeming is a no-op 200."""
    ada, acme, project_id = await _launch(client)
    link = await _mint(client, ada, project_id)
    r = await _accept(client, ada, link["code"])
    assert r.status_code == 200, r.text
    assert r.json() == {"project_id": project_id, "workspace_id": acme, "role": "owner"}
    unspent = await _listed(client, ada, project_id, link["id"])
    assert (unspent["uses"], unspent["state"]) == (0, "live")
    async with session_scope(workspace_id=uuid.UUID(acme)) as s:
        assert await ProjectMember.get(s, uuid.UUID(project_id), uuid.UUID(ada.id)) is None
    assert ada.id not in await _roles(client, ada, project_id)
    assert await _shared_items(client, ada) == []

    bob = await _signup(client, "Bob")  # the owner's redemption spent nothing: it still lets one in
    await _join(acme, bob, WorkspaceRole.MEMBER)
    assert (await _accept(client, bob, link["code"])).json()["role"] == "editor"


async def test_redeeming_while_another_workspace_is_active(client: AsyncClient) -> None:
    """Bob's session sits in his personal workspace when he redeems an Acme link. The
    membership, the knock (a tenant root, stamped from the BOUND workspace) and the session's active
    workspace must all be Acme's."""
    ada, acme, project_id = await _launch(client)
    link = await _mint(client, ada, project_id)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    assert await _active_workspace(client, bob) == bob.personal_workspace != acme

    r = await _accept(client, bob, link["code"])
    assert r.status_code == 200, r.text
    assert r.json() == {"project_id": project_id, "workspace_id": acme, "role": "editor"}

    async with session_scope() as s:
        member = await ProjectMember.get(s, uuid.UUID(project_id), uuid.UUID(bob.id))
        assert member is not None and member.role == "editor"
        knocks = list(
            await s.scalars(
                select(InboxItem).where(
                    InboxItem.actor_id == uuid.UUID(bob.id),
                    InboxItem.kind == InboxKind.PROJECT_SHARED.value,
                )
            )
        )
        assert [(k.workspace_id, k.project_id) for k in knocks] == [(uuid.UUID(acme), uuid.UUID(project_id))]
    assert await _active_workspace(client, bob) == acme


async def test_add_member_by_email_knocks_the_inbox(client: AsyncClient) -> None:
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _share(client, ada, project_id, bob, "viewer")
    await _switch(client, bob, acme)
    (knock,) = await _shared_items(client, bob)
    assert knock["title"] == "Ada added you to Launch" and knock["project_id"] == project_id
    assert knock["task_id"] is None


async def test_sharing_by_email_never_logs_the_email(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An address is personal data: the share and the transfer log who by id, never by email."""
    from prismoid.operations import projects as project_ops

    logged: list[str] = []

    class _Log:
        def info(self, template: str, **fields: Any) -> None:
            logged.append(template + " " + " ".join(str(v) for v in fields.values()))

    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    monkeypatch.setattr(project_ops, "logfire", _Log())
    await _share(client, ada, project_id, bob, "viewer")
    r = await client.post(f"/v1/projects/{project_id}/transfer", headers=ada.h, json={"email": bob.email})
    assert r.status_code == 200, r.text

    assert len(logged) == 2, logged
    assert not any(bob.email in line for line in logged), logged
    assert all(bob.id in line for line in logged), "who, by id"


async def test_the_link_shape_is_checked_at_the_door(client: AsyncClient) -> None:
    """Never an owner link; `expires_in_days` 1..30, and every link expires."""
    ada, _, project_id = await _launch(client)
    for body in (
        {"role": "owner"},
        {"role": "editor", "expires_in_days": 0},
        {"role": "editor", "expires_in_days": 31},
    ):
        r = await client.post(f"/v1/projects/{project_id}/invites", headers=ada.h, json=body)
        assert r.status_code == 422, f"{body}: {r.status_code} {r.text}"
    short = await _mint(client, ada, project_id, role="viewer", expires_in_days=1)
    ttl = dt.datetime.fromisoformat(short["expires_at"]) - utcnow()
    assert dt.timedelta(hours=23) < ttl <= dt.timedelta(days=1)
    assert short["role"] == "viewer"


async def test_the_use_count_is_not_the_callers_to_choose(client: AsyncClient) -> None:
    """Every link is one-time. A body that still asks for many uses (an
    older client) gets a single-use link like everyone else - the field is ignored, never honored."""
    ada, acme, project_id = await _launch(client)
    asked = await _mint(client, ada, project_id, max_uses=25)
    assert "max_uses" not in asked
    async with session_scope(workspace_id=uuid.UUID(acme)) as s:
        row = await s.get(ProjectInvite, uuid.UUID(asked["id"]))
        assert row is not None and row.max_uses == 1

    bob, cy = await _signup(client, "Bob"), await _signup(client, "Cy")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _join(acme, cy, WorkspaceRole.MEMBER)
    assert (await _accept(client, bob, asked["code"])).status_code == 200
    assert (await _accept(client, cy, asked["code"])).status_code == 404


async def test_a_link_lets_one_person_in_then_is_dead_for_the_next(client: AsyncClient) -> None:
    """Once a user accepts the invite, the invite link expires.
    Cy holds the same code Bob just used and is answered exactly as any dead code is."""
    ada, acme, project_id = await _launch(client)
    bob, cy = await _signup(client, "Bob"), await _signup(client, "Cy")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _join(acme, cy, WorkspaceRole.MEMBER)
    link = await _mint(client, ada, project_id)
    assert (await _accept(client, bob, link["code"])).status_code == 200

    r = await _accept(client, cy, link["code"])

    error = {k: v for k, v in r.json()["error"].items() if k != "request_id"}
    assert (r.status_code, error) == (404, {"code": "not_found", "message": _DEAD}), r.text
    assert cy.id not in await _roles(client, ada, project_id)
    assert await _knocks_in_db(cy) == 0
    assert await _active_workspace(client, cy) == cy.personal_workspace  # nothing moved for her
    spent = await _listed(client, ada, project_id, link["id"])
    assert (spent["uses"], spent["state"]) == (1, "used_up")


async def test_reopening_a_link_you_used_is_not_an_error(client: AsyncClient) -> None:
    """Bob opens his join link a second time (the Back button, a second tab): he is in, so he is
    answered with his role - nothing spent, nothing knocked, the link still reads used. To anyone
    not already in, the spent code stays exactly an unknown code: no oracle."""
    ada, acme, project_id = await _launch(client)
    bob, cy = await _signup(client, "Bob"), await _signup(client, "Cy")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _join(acme, cy, WorkspaceRole.MEMBER)
    link = await _mint(client, ada, project_id)
    assert (await _accept(client, bob, link["code"])).status_code == 200

    again = await _accept(client, bob, link["code"])

    assert again.status_code == 200, again.text
    assert again.json() == {"project_id": project_id, "workspace_id": acme, "role": "editor"}
    spent = await _listed(client, ada, project_id, link["id"])
    assert (spent["uses"], spent["state"]) == (1, "used_up")
    assert await _knocks_in_db(bob) == 1

    def answer(r: Response) -> tuple[int, dict[str, Any]]:
        return r.status_code, {k: v for k, v in r.json()["error"].items() if k != "request_id"}

    stale = await _accept(client, cy, link["code"])
    fake = await _accept(client, cy, f"{PROJECT_INVITE_PREFIX}{uuid.uuid4().hex}")
    assert answer(stale) == answer(fake) == (404, {"code": "not_found", "message": _DEAD})
    assert cy.id not in await _roles(client, ada, project_id)

    mine = await _accept(client, ada, link["code"])  # the owner holds the room: the same courtesy
    assert (mine.status_code, mine.json()["role"]) == (200, "owner")


async def test_a_revoked_link_stays_dead_even_for_the_person_it_let_in(client: AsyncClient) -> None:
    """Revoking is the owner's explicit kill, so it outranks the courtesy above: Bob, who joined
    through the link, gets the dead-code 404 once it is revoked (his membership is untouched)."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    link = await _mint(client, ada, project_id)
    assert (await _accept(client, bob, link["code"])).status_code == 200
    gone = await client.delete(f"/v1/projects/{project_id}/invites/{link['id']}", headers=ada.h)
    assert gone.status_code == 204, gone.text

    r = await _accept(client, bob, link["code"])

    assert (r.status_code, r.json()["error"]["message"]) == (404, _DEAD), r.text
    assert (await _roles(client, ada, project_id))[bob.id] == "editor"


async def test_a_spent_link_is_dead_to_its_joiner_once_the_owner_is_out_of_the_workspace(
    client: AsyncClient,
) -> None:
    """The owner-out-of-the-workspace rule wins over the re-open courtesy: the archived Project is not
    named back to anyone through a link."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    link = await _mint(client, ada, project_id)
    assert (await _accept(client, bob, link["code"])).status_code == 200
    async with session_scope() as s:
        await WorkspaceMember.remove(s, uuid.UUID(acme), uuid.UUID(ada.id))
        await s.commit()

    r = await _accept(client, bob, link["code"])

    assert (r.status_code, r.json()["error"]["message"]) == (404, _DEAD), r.text


async def test_a_removed_member_cannot_rejoin_with_the_link_they_used(client: AsyncClient) -> None:
    """The link was spent on Bob's join: once the owner removes him, it does not let him back."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    link = await _mint(client, ada, project_id)
    assert (await _accept(client, bob, link["code"])).status_code == 200
    removed = await client.delete(f"/v1/projects/{project_id}/members/{bob.id}", headers=ada.h)
    assert removed.status_code == 200, removed.text
    assert bob.id not in await _roles(client, ada, project_id)

    r = await _accept(client, bob, link["code"])

    assert (r.status_code, r.json()["error"]["message"]) == (404, _DEAD), r.text
    assert bob.id not in await _roles(client, ada, project_id)
    async with session_scope(workspace_id=uuid.UUID(acme)) as s:
        assert await ProjectMember.get(s, uuid.UUID(project_id), uuid.UUID(bob.id)) is None


def _api_routes(routes: Iterable[BaseRoute]) -> list[BaseRoute]:
    """Every APIRoute, nesting included - FastAPI 0.139 mounts an included router as one
    `_IncludedRouter` node reachable only through `.original_router` (test_workspace_isolation's walk)."""
    found: list[BaseRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            nested = getattr(child, "routes", child)
            if nested:
                found.extend(_api_routes(nested))
    return found


def test_the_accept_body_never_reaches_a_span() -> None:
    """The si_ code is the accept route's body: its parsed arguments are dropped from traces. The
    route is the MOUNTED one, found by its view - a moved path fails here, not in production."""
    from types import SimpleNamespace

    (route,) = [
        r
        for r in _api_routes(create_app().routes)
        if isinstance(r, APIRoute) and r.endpoint is project_invite_views.accept_project_invite
    ]
    request = SimpleNamespace(scope={"route": route})
    attributes = {"values": {"body": {"code": f"{PROJECT_INVITE_PREFIX}secret"}}}
    dropped = app_module.drop_route_arguments(request, attributes)  # pyright: ignore[reportArgumentType]
    assert dropped["values"] == {}, route.path


async def _assert_nothing_moved(
    client: AsyncClient, r: Response, acme: str, project_id: str, link: dict[str, Any], who: Person
) -> None:
    """The accept answered as an unknown code would, spent nothing, granted nothing, knocked
    nobody, and left the caller's session where it was."""
    assert r.status_code == 404, r.text
    error = {k: v for k, v in r.json()["error"].items() if k != "request_id"}
    assert (r.status_code, error) == (404, {"code": "not_found", "message": _DEAD}), r.text
    async with session_scope(workspace_id=uuid.UUID(acme)) as s:
        row = await s.get(ProjectInvite, uuid.UUID(link["id"]))
        assert row is not None and row.uses == 0
        assert await ProjectMember.get(s, uuid.UUID(project_id), uuid.UUID(who.id)) is None
    assert await _knocks_in_db(who) == 0
    assert await _active_workspace(client, who) == who.personal_workspace


async def test_a_link_dies_when_its_projects_owner_is_out_of_the_workspace(client: AsyncClient) -> None:
    """A departing person's PRIVATE Project stays theirs and is archived : nobody in the workspace
    may open it. A link Ada minted before she left must not be the
    way in - it answers as any dead code. Her membership row is removed directly: that is the
    state the archive leaves (the person out, the private Project still theirs), whichever removal
    path got there."""
    ada, acme, project_id = await _launch(client)
    link = await _mint(client, ada, project_id)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    async with session_scope() as s:
        await WorkspaceMember.remove(s, uuid.UUID(acme), uuid.UUID(ada.id))
        await s.commit()

    r = await _accept(client, bob, link["code"])

    await _assert_nothing_moved(client, r, acme, project_id, link, bob)


async def test_a_link_dies_while_its_projects_owner_is_suspended(client: AsyncClient) -> None:
    """A suspended account acts nowhere (the kill switch), so its links let nobody in either -
    the channel door's rule for a suspended owner."""
    ada, acme, project_id = await _launch(client)
    link = await _mint(client, ada, project_id)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    async with session_scope() as s:
        await s.execute(
            update(Actor).where(Actor.id == uuid.UUID(ada.id)).values(status=ActorStatus.SUSPENDED.value)
        )
        await s.commit()

    r = await _accept(client, bob, link["code"])

    await _assert_nothing_moved(client, r, acme, project_id, link, bob)


# ── races (fix round 1): the invite row lock and the raise-only upsert ────────────────────────


async def test_the_last_use_has_one_winner_over_http(client: AsyncClient) -> None:
    """Two people race for one link: exactly one joins, the other gets the dead-code 404."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    cy = await _signup(client, "Cy")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _join(acme, cy, WorkspaceRole.MEMBER)
    link = await _mint(client, ada, project_id)

    answers = await _together(_accept(client, bob, link["code"]), _accept(client, cy, link["code"]))

    assert sorted(r.status_code for r in answers) == [200, 404], [r.text for r in answers]
    (lost,) = [r for r in answers if r.status_code == 404]
    assert lost.json()["error"]["message"] == _DEAD
    roles = await _roles(client, ada, project_id)
    assert len({bob.id, cy.id} & roles.keys()) == 1, roles
    assert (await _listed(client, ada, project_id, link["id"]))["uses"] == 1


async def test_a_double_submit_joins_once(client: AsyncClient) -> None:
    """One person's two concurrent accepts of one link serialize on the invite row: the first
    spends the link, the second finds it spent with Bob already in and answers him with his role
    (re-opening a link you used is not an error) - one membership, one use, one knock."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    link = await _mint(client, ada, project_id)

    answers = await _together(_accept(client, bob, link["code"]), _accept(client, bob, link["code"]))

    assert [r.status_code for r in answers] == [200, 200], [r.text for r in answers]
    assert {r.json()["role"] for r in answers} == {"editor"}
    assert (await _roles(client, ada, project_id))[bob.id] == "editor"
    assert (await _listed(client, ada, project_id, link["id"]))["uses"] == 1
    assert await _knocks_in_db(bob) == 1


async def test_a_viewer_link_and_an_editor_link_at_once_end_as_editor(client: AsyncClient) -> None:
    """Two links, one person, at once: whichever lands last, the upsert never lowers a role."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    viewer_link = await _mint(client, ada, project_id, role="viewer")
    editor_link = await _mint(client, ada, project_id, role="editor")

    answers = await _together(
        _accept(client, bob, viewer_link["code"]), _accept(client, bob, editor_link["code"])
    )

    assert [r.status_code for r in answers] == [200, 200], [r.text for r in answers]
    assert answers[1].json()["role"] == "editor"
    assert (await _roles(client, ada, project_id))[bob.id] == "editor"
    async with session_scope(workspace_id=uuid.UUID(acme)) as s:
        (row,) = list(
            await s.scalars(select(ProjectMember).where(ProjectMember.project_id == uuid.UUID(project_id)))
        )
        assert (row.actor_id, row.role) == (uuid.UUID(bob.id), "editor")


async def test_the_owner_still_lowers_a_role_by_email(client: AsyncClient) -> None:
    """add_member is the owner's explicit set: it upserts too, but moves a role either way."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _share(client, ada, project_id, bob, "editor")
    await _share(client, ada, project_id, bob, "viewer")
    assert (await _roles(client, ada, project_id))[bob.id] == "viewer"


async def test_a_member_reads_the_owner_by_name_on_every_project_read(client: AsyncClient) -> None:
    """The owner is a column, not a member row, so the members list never names them. The Project read
    does: Bob, who joined by link, sees who owns Launch - on the read, and on the
    owner's own create / patch answers, which are the same shape."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    assert (await _accept(client, bob, (await _mint(client, ada, project_id))["code"])).status_code == 200
    ada_as_owner = {"user_id": ada.id, "name": "Ada", "email": ada.email}

    read = await client.get(f"/v1/projects/{project_id}", headers=bob.h)
    assert read.status_code == 200, read.text
    assert read.json()["owner"] == ada_as_owner
    assert read.json()["role"] == "editor"

    patched = await client.patch(f"/v1/projects/{project_id}", headers=ada.h, json={"name": "Launch 2"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["owner"] == ada_as_owner

    made = await client.post("/v1/projects", headers=ada.h, json={"name": "Second"})
    assert made.status_code == 201, made.text
    assert made.json()["owner"] == ada_as_owner

    listed = await client.get("/v1/projects", headers=bob.h)
    assert all("owner" not in row for row in listed.json()["items"]), "the list stays a cheap page"


async def test_the_task_list_names_who_asked_for_the_lobby(client: AsyncClient) -> None:
    """Live now is the Project's task list: each row carries its creator's and its turn author's
    names, so the page never asks a second question per task."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    assert (await _accept(client, bob, (await _mint(client, ada, project_id))["code"])).status_code == 200
    for person, goal in ((ada, "Ada's research"), (bob, "Bob's memo")):
        made = await client.post("/v1/tasks", headers=person.h, json={"goal": goal, "project_id": project_id})
        assert made.status_code in (200, 201, 202), made.text

    r = await client.get("/v1/tasks", headers=bob.h, params={"project_id": project_id})
    assert r.status_code == 200, r.text
    rows = {t["goal"]: (t["creator_name"], t["turn_author_name"]) for t in r.json()["items"]}
    assert rows == {"Ada's research": ("Ada", "Ada"), "Bob's memo": ("Bob", "Bob")}


async def test_transferring_a_project_answers_with_the_new_owner(client: AsyncClient) -> None:
    """The transfer's own answer names the NEW owner (not the caller who just gave the room away),
    and the old owner reads as an editor - the same shape every other Project read carries."""
    ada, acme, project_id = await _launch(client)
    bob = await _signup(client, "Bob")
    await _join(acme, bob, WorkspaceRole.MEMBER)

    r = await client.post(f"/v1/projects/{project_id}/transfer", headers=ada.h, json={"email": bob.email})

    assert r.status_code == 200, r.text
    assert r.json()["owner"] == {"user_id": bob.id, "name": "Bob", "email": bob.email}
    assert r.json()["owner_id"] == bob.id
    assert r.json()["role"] == "editor"
    again = await client.get(f"/v1/projects/{project_id}", headers=ada.h)
    assert again.json()["owner"]["name"] == "Bob" and again.json()["role"] == "editor"


async def test_a_guest_who_joins_by_link_knows_the_project_by_names_only(client: AsyncClient) -> None:
    """A workspace guest sees only themselves in the workspace's roster (#8), so a Project they join by link
    must not hand them everyone's email instead: its owner and members read by name, never by
    email - the detail, the member list, and a member row the owner reads back the same way when
    the owner is a guest. A member's reads are unchanged."""
    ada, acme, project_id = await _launch(client)
    bob, gus = await _signup(client, "Bob"), await _signup(client, "Gus")
    await _join(acme, bob, WorkspaceRole.MEMBER)
    await _join(acme, gus, WorkspaceRole.GUEST)
    await _switch(client, bob, acme)
    await _share(client, ada, project_id, bob, "editor")
    minted = await _mint(client, ada, project_id, role="viewer")
    assert (await _accept(client, gus, minted["code"])).status_code == 200  # in Acme now
    everyone = (ada.email, bob.email, gus.email)

    detail = await client.get(f"/v1/projects/{project_id}", headers=gus.h)
    members = await client.get(f"/v1/projects/{project_id}/members", headers=gus.h)
    assert detail.status_code == members.status_code == 200
    assert not any(e in detail.text + members.text for e in everyone), "no email reaches a guest"
    assert detail.json()["owner"] == {"user_id": ada.id, "name": "Ada", "email": None}
    assert {(m["name"], m["email"]) for m in members.json()} == {("Bob", None), ("Gus", None)}

    detail = await client.get(f"/v1/projects/{project_id}", headers=bob.h)
    members = await client.get(f"/v1/projects/{project_id}/members", headers=bob.h)
    assert detail.json()["owner"]["email"] == ada.email, "a member's read is unchanged"
    assert {(m["name"], m["email"]) for m in members.json()} == {("Bob", bob.email), ("Gus", gus.email)}
