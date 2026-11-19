# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Workspace routes (ADR-061): members, roles, invites, transfer - through HTTP.

Owner/admin/member/guest gates use the Project.get_for split (a stranger 404s, a member below the
role 403s); a workspace is never ownerless; an invite is the invitee's alone;
an owner is made by transfer, never by invite.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from prismoid.api.app import create_app
from prismoid.conf import settings
from prismoid.db.models.project import Project
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceMember, WorkspaceRole, WorkspaceStatus
from prismoid.db.models.workspace_invite import WorkspaceInvite
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import inbox as inbox_ops
from prismoid.schemas.ids import WORKSPACE_INVITE_PREFIX
from tests.support.cast import verify_email

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"


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
    client: AsyncClient,
    name: str,
    *,
    invite_code: str | None = None,
    email: str | None = None,
    verified: bool = True,
) -> Person:
    email = email or f"workspaces{uuid.uuid4().hex[:12]}@example.com"
    body: dict[str, object] = {"email": email, "name": name, "password": _PASSWORD}
    if invite_code:
        body["invite_code"] = invite_code
    r = await client.post("/v1/auth/signup", json=body)
    assert r.status_code == 201, r.text
    await auth_ops.flush_outbound_email()  # the verification mail lands now, never in a later capture
    token = r.json()["token"]
    me = await client.get("/v1/auth/me", headers=_h(token))
    if verified:  # an inviter's address is proven (ADR-086); an invitee's is already
        await verify_email(me.json()["id"])
    return Person(token, me.json(), email)


async def _team(client: AsyncClient, owner: Person, name: str = "Acme") -> str:
    r = await client.post("/v1/workspaces", headers=owner.h, json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _join(workspace_id: str, person: Person, role: WorkspaceRole) -> None:
    async with session_scope() as s:
        await WorkspaceMember.add(s, uuid.UUID(workspace_id), uuid.UUID(person.id), role)
        await s.commit()


async def _role_of(workspace_id: str, person: Person) -> str | None:
    async with session_scope() as s:
        m = await WorkspaceMember.get(s, uuid.UUID(workspace_id), uuid.UUID(person.id))
        return m.role if m else None


async def _console_code(capsys: pytest.CaptureFixture[str]) -> str:
    await auth_ops.flush_outbound_email()
    out = capsys.readouterr().out
    found = re.findall(rf"{WORKSPACE_INVITE_PREFIX}[A-Za-z0-9_\-]+", out)
    assert found, f"no invite code printed on the console:\n{out}"
    return found[-1]


async def test_owner_creates_a_team_workspace(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    r = await client.post("/v1/workspaces", headers=ada.h, json={"name": "Acme Labs"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["kind"], body["role"]) == ("team", "owner")
    mine = {o["id"]: o for o in (await client.get("/v1/workspaces", headers=ada.h)).json()}
    assert mine[body["id"]]["role"] == "owner"
    assert (await client.get(f"/v1/workspaces/{body['id']}", headers=ada.h)).json()["name"] == "Acme Labs"


async def test_an_admin_invites_and_the_invitee_signs_up_into_the_team(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    bob_email = f"bob{uuid.uuid4().hex[:8]}@example.com"
    capsys.readouterr()
    r = await client.post(
        f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": bob_email, "role": "member"}
    )
    assert r.status_code == 201, r.text
    assert "code" not in r.json()
    await auth_ops.flush_outbound_email()
    mail = capsys.readouterr().out
    # The email names the two places the web takes a code.
    assert "Invite code field" in mail and "Join with a code" in mail, mail
    code = re.findall(rf"{WORKSPACE_INVITE_PREFIX}[A-Za-z0-9_\-]+", mail)[-1]
    bob = await _signup(client, "Bob", invite_code=code, email=bob_email)
    me = (await client.get("/v1/auth/me", headers=bob.h)).json()
    assert (me["workspace"]["id"], me["workspace"]["role"]) == (workspace, "member")
    mine = {o["id"]: o["kind"] for o in (await client.get("/v1/workspaces", headers=bob.h)).json()}
    assert set(mine.values()) == {"personal", "team"}
    # single use
    again = await client.post("/v1/workspaces/invites/accept", headers=bob.h, json={"code": code})
    assert again.status_code == 404


async def test_an_unverified_person_cannot_send_an_invite(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Open signup must not make invites a mail cannon: the mail carries names the sender chose."""
    ada = await _signup(client, "Ada", verified=False)
    workspace = await _team(client, ada)
    capsys.readouterr()
    r = await client.post(
        f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": "x@example.com"}
    )
    assert (r.status_code, r.json()["error"]["message"]) == (
        403,
        "Verify your email before you invite anyone.",
    )
    await auth_ops.flush_outbound_email()
    assert WORKSPACE_INVITE_PREFIX not in capsys.readouterr().out
    async with session_scope() as s:
        assert await WorkspaceInvite.live_for_workspace(s, uuid.UUID(workspace)) == []

    await verify_email(ada.id)
    r = await client.post(
        f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": "x@example.com"}
    )
    assert r.status_code == 201, r.text


async def test_invites_are_capped_per_person_per_hour(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.workspaces, "invite_max_per_hour", 2)
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    url = f"/v1/workspaces/{workspace}/invites"
    for n in range(2):
        r = await client.post(url, headers=ada.h, json={"email": f"p{n}{uuid.uuid4().hex[:6]}@example.com"})
        assert r.status_code == 201, r.text
    r = await client.post(url, headers=ada.h, json={"email": "third@example.com"})
    assert r.status_code == 429, r.text
    async with session_scope() as s:
        assert len(await WorkspaceInvite.live_for_workspace(s, uuid.UUID(workspace))) == 2


async def test_signup_with_someone_elses_invite_is_refused_and_creates_nothing(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    await client.post(
        f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": "someone@example.com"}
    )
    code = await _console_code(capsys)
    email = f"eve{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/v1/auth/signup", json={"email": email, "name": "Eve", "password": _PASSWORD, "invite_code": code}
    )
    assert r.status_code == 403
    login = await client.post("/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 401  # the whole signup rolled back


async def _signup_status(client: AsyncClient, email: str, code: str | None = None) -> int:
    body: dict[str, object] = {"email": email, "name": "Invitee", "password": _PASSWORD}
    if code is not None:
        body["invite_code"] = code
    return (await client.post("/v1/auth/signup", json=body)).status_code


async def _has_account(email: str) -> bool:
    async with session_scope() as s:
        return await User.by_email(s, email) is not None


async def test_invite_only_signup_lets_the_invitee_in_and_nobody_else(
    client: AsyncClient, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With signup off (invite-only): a live workspace invite for THIS address passes the switch, and
    nothing else does - no code, a garbage code, or another person's code is refused before any row."""
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    bob_email = f"bob{uuid.uuid4().hex[:8]}@example.com"
    await client.post(
        f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": bob_email, "role": "member"}
    )
    code = await _console_code(capsys)
    monkeypatch.setattr(settings.auth, "signup_enabled", False)

    eve = f"eve{uuid.uuid4().hex[:8]}@example.com"
    assert await _signup_status(client, eve) == 403
    assert await _signup_status(client, eve, f"{WORKSPACE_INVITE_PREFIX}not-a-real-code") == 404
    assert await _signup_status(client, eve, code) == 403  # Bob's code, Eve's address
    assert not await _has_account(eve)

    assert await _signup_status(client, bob_email, code) == 201
    login = await client.post("/v1/auth/login", json={"email": bob_email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    me = (await client.get("/v1/auth/me", headers=_h(login.json()["token"]))).json()
    async with session_scope() as s:
        assert await WorkspaceMember.get(s, uuid.UUID(workspace), uuid.UUID(me["id"])) is not None
    # Spent: the code lets nobody else in afterwards.
    assert await _signup_status(client, f"x{bob_email}", code) == 404


async def test_another_logged_in_person_cannot_spend_the_invite(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """A different email is a 403 and the invite stays pending."""
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    bob_email = f"bob{uuid.uuid4().hex[:8]}@example.com"
    await client.post(f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": bob_email})
    code = await _console_code(capsys)
    carol = await _signup(client, "Carol")
    r = await client.post("/v1/workspaces/invites/accept", headers=carol.h, json={"code": code})
    assert r.status_code == 403
    assert await _role_of(workspace, carol) is None
    listed = (await client.get(f"/v1/workspaces/{workspace}/invites", headers=ada.h)).json()
    assert [i["email"] for i in listed] == [bob_email]
    bob = await _signup(client, "Bob", email=bob_email)  # the rightful invitee still can
    ok = await client.post("/v1/workspaces/invites/accept", headers=bob.h, json={"code": code})
    assert ok.status_code == 200 and ok.json()["id"] == workspace
    me = (await client.get("/v1/auth/me", headers=bob.h)).json()
    assert me["workspace"]["id"] == workspace  # the session followed


async def test_role_gates_use_the_404_403_split(client: AsyncClient) -> None:
    ada, mia, gus, out = [await _signup(client, n) for n in ("Ada", "Mia", "Gus", "Out")]
    workspace = await _team(client, ada)
    await _join(workspace, mia, WorkspaceRole.MEMBER)
    await _join(workspace, gus, WorkspaceRole.GUEST)
    invite = {"email": "new@example.com", "role": "member"}
    assert (
        await client.post(f"/v1/workspaces/{workspace}/invites", headers=mia.h, json=invite)
    ).status_code == 403
    assert (await client.get(f"/v1/workspaces/{workspace}/invites", headers=mia.h)).status_code == 403
    assert (
        await client.patch(f"/v1/workspaces/{workspace}", headers=mia.h, json={"name": "x"})
    ).status_code == 403
    assert (
        await client.post(f"/v1/workspaces/{workspace}/transfer", headers=mia.h, json={"actor_id": mia.id})
    ).status_code == 403
    # a stranger learns nothing
    for method, path in (
        ("GET", ""),
        ("GET", "/members"),
        ("GET", "/invites"),
        ("POST", "/members/me/leave"),
    ):
        r = await client.request(method, f"/v1/workspaces/{workspace}{path}", headers=out.h)
        assert r.status_code == 404, (method, path, r.status_code)
    # a guest sees only themselves; a member sees everyone
    guest_view = (await client.get(f"/v1/workspaces/{workspace}/members", headers=gus.h)).json()
    assert [m["user"]["id"] for m in guest_view] == [gus.id]
    assert len((await client.get(f"/v1/workspaces/{workspace}/members", headers=mia.h)).json()) == 3


async def test_founding_workspaces_is_throttled_per_person(client: AsyncClient) -> None:
    """Every new workspace carries the month's grant: one person founds a few a minute, not a farm."""
    ada, bob = await _signup(client, "Ada"), await _signup(client, "Bob")
    limit = math.ceil(settings.workspaces.create_rate_limit_per_minute / settings.api.replica_count)
    for n in range(limit):
        await _team(client, ada, f"Farm {n}")
    refused = await client.post("/v1/workspaces", headers=ada.h, json={"name": "One too many"})
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
    await _team(client, bob, "Bob's own")  # a bucket per person


async def test_owner_renames_and_reslugs_and_a_taken_slug_conflicts(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    one, two = await _team(client, ada, "One"), await _team(client, ada, "Two")
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    r = await client.patch(f"/v1/workspaces/{one}", headers=ada.h, json={"name": "Renamed", "slug": slug})
    assert r.status_code == 200 and (r.json()["name"], r.json()["slug"]) == ("Renamed", slug)
    assert (
        await client.patch(f"/v1/workspaces/{two}", headers=ada.h, json={"slug": slug})
    ).status_code == 409
    assert (
        await client.patch(f"/v1/workspaces/{two}", headers=ada.h, json={"slug": "Not Valid"})
    ).status_code == 422


async def test_a_whitespace_only_name_is_refused_after_stripping(client: AsyncClient) -> None:
    """ "   " passes min_length and strips to nothing: create and rename both answer 422 and the
    workspace keeps its name."""
    ada = await _signup(client, "Ada")
    made = await client.post("/v1/workspaces", headers=ada.h, json={"name": "   "})
    assert made.status_code == 422, made.text
    one = await _team(client, ada, "Kept")
    renamed = await client.patch(f"/v1/workspaces/{one}", headers=ada.h, json={"name": " \t "})
    assert renamed.status_code == 422, renamed.text
    assert (await client.get(f"/v1/workspaces/{one}", headers=ada.h)).json()["name"] == "Kept"


async def test_signup_refuses_explicitly_when_its_actor_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production path never leans on `assert` (stripped under -O): a signup whose actor
    cannot be read back raises the house error and makes no ownerless workspace."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from prismoid.db.models.actor import Actor
    from prismoid.errors import PrismoidError

    real_get = AsyncSession.get

    async def get(self: AsyncSession, model: object, ident: object, **kw: object) -> object:
        return None if model is Actor else await real_get(self, model, ident, **kw)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(AsyncSession, "get", get)
    email = f"ghost{uuid.uuid4().hex[:8]}@example.com"
    async with session_scope() as s:
        with pytest.raises(PrismoidError, match="no actor"):
            await auth_ops.signup(s, email=email, name="Ghost", password="a-long-enough-password")


async def test_the_last_owner_cannot_leave_be_demoted_or_be_removed(client: AsyncClient) -> None:
    """A workspace is never ownerless."""
    ada, adm = await _signup(client, "Ada"), await _signup(client, "Adm")
    workspace = await _team(client, ada)
    await _join(workspace, adm, WorkspaceRole.ADMIN)
    assert (
        await client.post(f"/v1/workspaces/{workspace}/members/me/leave", headers=ada.h)
    ).status_code == 409
    demote = await client.patch(
        f"/v1/workspaces/{workspace}/members/{ada.id}", headers=ada.h, json={"role": "member"}
    )
    assert demote.status_code == 409
    assert (
        await client.delete(f"/v1/workspaces/{workspace}/members/{ada.id}", headers=adm.h)
    ).status_code == 409
    assert (
        await client.delete(f"/v1/workspaces/{workspace}/members/{ada.id}", headers=ada.h)
    ).status_code == 409
    assert await _role_of(workspace, ada) == "owner"
    # after a transfer the old owner is an admin and may leave
    moved = await client.post(
        f"/v1/workspaces/{workspace}/transfer", headers=ada.h, json={"actor_id": adm.id}
    )
    assert moved.status_code == 200, moved.text
    assert (await _role_of(workspace, adm), await _role_of(workspace, ada)) == ("owner", "admin")
    assert (
        await client.post(f"/v1/workspaces/{workspace}/members/me/leave", headers=ada.h)
    ).status_code == 204
    assert await _role_of(workspace, ada) is None


async def _suspend(workspace_id: str) -> None:
    async with session_scope() as s:
        await s.execute(
            update(Workspace)
            .where(Workspace.id == uuid.UUID(workspace_id))
            .values(status=WorkspaceStatus.SUSPENDED.value)
        )
        await s.commit()


async def test_nobody_leaves_their_last_workspace(client: AsyncClient) -> None:
    """Leaving the last active membership would leave the person nowhere to act:
    a 409, and they stay. A membership in a suspended workspace does not count as somewhere."""
    ada, bob = await _signup(client, "Ada"), await _signup(client, "Bob")
    workspace = await _team(client, ada)
    await _join(workspace, bob, WorkspaceRole.MEMBER)
    await _suspend(bob.personal_workspace)
    refused = await client.post(f"/v1/workspaces/{workspace}/members/me/leave", headers=bob.h)
    assert refused.status_code == 409, refused.text
    assert await _role_of(workspace, bob) == "member"


async def test_two_leaves_at_once_never_leave_a_person_nowhere(client: AsyncClient) -> None:
    """The last-membership guard under a race: one person leaving their last two workspaces at once
    is serialized on their membership rows, so exactly one leave wins."""
    ada, bob = await _signup(client, "Ada"), await _signup(client, "Bob")
    first, second = await _team(client, ada, "One"), await _team(client, ada, "Two")
    await _join(first, bob, WorkspaceRole.MEMBER)
    await _join(second, bob, WorkspaceRole.MEMBER)
    await _suspend(bob.personal_workspace)
    rs = await asyncio.gather(
        client.post(f"/v1/workspaces/{first}/members/me/leave", headers=bob.h),
        client.post(f"/v1/workspaces/{second}/members/me/leave", headers=bob.h),
    )
    assert sorted(r.status_code for r in rs) == [204, 409], [r.text for r in rs]
    assert [await _role_of(o, bob) for o in (first, second)].count("member") == 1


async def test_owner_roles_are_changed_by_owners_only(client: AsyncClient) -> None:
    ada, co, adm, mia = [await _signup(client, n) for n in ("Ada", "Co", "Adm", "Mia")]
    workspace = await _team(client, ada)
    await _join(workspace, co, WorkspaceRole.OWNER)
    await _join(workspace, adm, WorkspaceRole.ADMIN)
    await _join(workspace, mia, WorkspaceRole.MEMBER)
    base = f"/v1/workspaces/{workspace}/members"
    assert (await client.patch(f"{base}/{co.id}", headers=adm.h, json={"role": "member"})).status_code == 403
    assert (await client.delete(f"{base}/{co.id}", headers=adm.h)).status_code == 403
    assert (await client.patch(f"{base}/{mia.id}", headers=adm.h, json={"role": "owner"})).status_code == 403
    assert (await client.patch(f"{base}/{mia.id}", headers=mia.h, json={"role": "admin"})).status_code == 403
    ok = await client.patch(f"{base}/{mia.id}", headers=adm.h, json={"role": "guest"})
    assert ok.status_code == 200 and ok.json()["role"] == "guest"
    assert (await client.patch(f"{base}/{co.id}", headers=ada.h, json={"role": "member"})).status_code == 200


async def test_an_owner_is_made_by_transfer_never_by_invite(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    r = await client.post(
        f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": "x@example.com", "role": "owner"}
    )
    assert r.status_code == 422
    assert (await client.get(f"/v1/workspaces/{workspace}/invites", headers=ada.h)).json() == []


async def test_a_removed_member_loses_the_key_and_the_session_rebinds(client: AsyncClient) -> None:
    ada, bob = await _signup(client, "Ada"), await _signup(client, "Bob")
    workspace = await _team(client, ada)
    await _join(workspace, bob, WorkspaceRole.MEMBER)
    assert (
        await client.put("/v1/auth/session/workspace", headers=bob.h, json={"workspace_id": workspace})
    ).status_code == 200
    key = (await client.post("/v1/auth/api-keys", headers=bob.h, json={"name": "k"})).json()["key"]
    assert (await client.get("/v1/auth/me", headers=_h(key))).status_code == 200
    r = await client.delete(f"/v1/workspaces/{workspace}/members/{bob.id}", headers=ada.h)
    assert r.status_code == 204, r.text
    assert (await client.get("/v1/auth/me", headers=_h(key))).status_code == 401
    me = await client.get("/v1/auth/me", headers=bob.h)
    assert me.status_code == 200 and me.json()["workspace"]["id"] == bob.personal_workspace


async def _project_in(client: AsyncClient, owner: Person, workspace: str, name: str) -> str:
    assert (
        await client.put("/v1/auth/session/workspace", headers=owner.h, json={"workspace_id": workspace})
    ).status_code == 200
    r = await client.post("/v1/projects", headers=owner.h, json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _shared_project_in(
    client: AsyncClient, owner: Person, workspace: str, name: str, *, with_: Person
) -> str:
    project = await _project_in(client, owner, workspace, name)
    shared = await client.post(
        f"/v1/projects/{project}/members", headers=owner.h, json={"email": with_.email}
    )
    assert shared.status_code == 201, shared.text
    return project


async def _task_in(client: AsyncClient, creator: Person, project: str) -> str:
    body = {"goal": "a private note", "clarify": "never", "budget": {"usd": "1"}, "project_id": project}
    r = await client.post("/v1/tasks", headers=creator.h, json=body)
    assert r.status_code == 202, r.text
    return str(r.json()["task_id"])


async def _project_owner(project: str) -> str:
    async with session_scope(platform=True) as s:
        owner = await s.scalar(select(Project.owner_actor_id).where(Project.id == uuid.UUID(project)))
    return str(owner)


async def _knock_completed(workspace: str, task: str) -> None:
    """The task's own completion knock, as the run would send it: addressed to its creator."""
    async with session_scope(workspace_id=uuid.UUID(workspace)) as s:
        row = await s.get(Task, uuid.UUID(task))
        assert row is not None
        await inbox_ops.task_completed(s, row, "done")
        await s.commit()


async def _use(client: AsyncClient, person: Person, workspace: str) -> None:
    assert (
        await client.put("/v1/auth/session/workspace", headers=person.h, json={"workspace_id": workspace})
    ).status_code == 200


async def _assert_hidden(
    client: AsyncClient, person: Person, workspace: str, project: str, task: str
) -> None:
    """Every door a workspace admin or owner could knock on answers as for a Project that is not there."""
    await _use(client, person, workspace)
    h = person.h
    for path in ("", "/members", "/artifacts", "/usage"):
        r = await client.get(f"/v1/projects/{project}{path}", headers=h)
        assert r.status_code == 404, (path, r.status_code, r.text)
    assert (
        await client.patch(f"/v1/projects/{project}", headers=h, json={"budget_usd": "5"})
    ).status_code == 404
    transfer = await client.post(f"/v1/projects/{project}/transfer", headers=h, json={"email": person.email})
    assert transfer.status_code == 404
    listed = (await client.get("/v1/projects", headers=h)).json()["items"]
    assert project not in {s["id"] for s in listed}
    assert (await client.get(f"/v1/tasks/{task}", headers=h)).status_code == 404
    in_project = await client.get("/v1/tasks", headers=h, params={"project_id": project})
    assert in_project.status_code == 200 and in_project.json()["items"] == []
    assert task not in {t["id"] for t in (await client.get("/v1/tasks", headers=h)).json()["items"]}
    inbox = (await client.get("/v1/inbox", headers=h)).json()["items"]
    assert task not in {i.get("task_id") for i in inbox}
    filed = await client.post(
        "/v1/tasks",
        headers=h,
        json={"goal": "x", "clarify": "never", "budget": {"usd": "1"}, "project_id": project},
    )
    assert filed.status_code == 404, filed.text


async def test_removal_hands_shared_projects_to_the_admin_and_archives_private_ones(
    client: AsyncClient,
) -> None:
    """Workspace roles grant nothing inside a Project, so a SHARED Project owned by someone who
    was removed would be frozen for good: it passes to the admin who removed them, its members
    and contents as they were. A PRIVATE Project (no members) is archived instead: it stays the
    departed person's, so no admin or owner of the
    workspace can open it or its tasks, and it is theirs again if they rejoin."""
    ada, adm, bob, cy = [await _signup(client, n) for n in ("Ada", "Adm", "Bob", "Cy")]
    workspace = await _team(client, ada)
    await _join(workspace, adm, WorkspaceRole.ADMIN)
    for person in (bob, cy):
        await _join(workspace, person, WorkspaceRole.MEMBER)
    shared = await _shared_project_in(client, bob, workspace, "Bob's team notes", with_=cy)
    private = await _project_in(client, bob, workspace, "Bob's private notes")
    task = await _task_in(client, bob, private)

    assert (
        await client.delete(f"/v1/workspaces/{workspace}/members/{bob.id}", headers=adm.h)
    ).status_code == 204

    await _use(client, adm, workspace)
    got = await client.get(f"/v1/projects/{shared}", headers=adm.h)
    assert got.status_code == 200 and (got.json()["owner_id"], got.json()["role"]) == (adm.id, "owner")
    assert (
        await client.patch(f"/v1/projects/{shared}", headers=adm.h, json={"budget_usd": "5"})
    ).status_code == 200
    roster = (await client.get(f"/v1/projects/{shared}/members", headers=adm.h)).json()
    assert [m["actor_id"] for m in roster] == [cy.id]  # the share stands; the heir is the column

    assert await _project_owner(private) == bob.id  # archived: nobody inherited it
    await _knock_completed(workspace, task)  # a late knock still goes to its creator, never the admin
    for person in (adm, ada, cy):  # the removing admin, the workspace's owner, a fellow member
        await _assert_hidden(client, person, workspace, private, task)

    await _join(workspace, bob, WorkspaceRole.MEMBER)  # back in: the private Project is theirs again
    await _use(client, bob, workspace)
    mine = await client.get(f"/v1/projects/{private}", headers=bob.h)
    assert mine.status_code == 200 and (mine.json()["owner_id"], mine.json()["role"]) == (bob.id, "owner")
    assert private in {s["id"] for s in (await client.get("/v1/projects", headers=bob.h)).json()["items"]}
    assert (await client.get(f"/v1/tasks/{task}", headers=bob.h)).status_code == 200
    inbox = (await client.get("/v1/inbox", headers=bob.h)).json()["items"]
    assert task in {i.get("task_id") for i in inbox}
    # The shared one stays with its heir: a hand-over is not undone by a return.
    assert (await client.get(f"/v1/projects/{shared}", headers=bob.h)).status_code == 404


async def test_leaving_hands_shared_projects_to_the_owner_and_archives_private_ones(
    client: AsyncClient,
) -> None:
    """The same split for a person who leaves: shared Projects pass to the workspace's owner, private
    ones stay theirs, out of every workspace member's sight."""
    ada, adm, cy, dee = [await _signup(client, n) for n in ("Ada", "Adm", "Cy", "Dee")]
    workspace = await _team(client, ada)
    await _join(workspace, adm, WorkspaceRole.ADMIN)
    for person in (cy, dee):
        await _join(workspace, person, WorkspaceRole.MEMBER)
    shared = await _shared_project_in(client, cy, workspace, "Cy's team plans", with_=dee)
    private = await _project_in(client, cy, workspace, "Cy's private plans")
    task = await _task_in(client, cy, private)
    own = await _project_in(client, ada, workspace, "Ada's own")

    assert (
        await client.post(f"/v1/workspaces/{workspace}/members/me/leave", headers=cy.h)
    ).status_code == 204

    await _use(client, ada, workspace)
    got = await client.get(f"/v1/projects/{shared}", headers=ada.h)
    assert got.status_code == 200 and (got.json()["owner_id"], got.json()["role"]) == (ada.id, "owner")
    assert await _project_owner(private) == cy.id
    assert await _project_owner(own) == ada.id  # an owner keeps what is already theirs
    for person in (ada, adm, dee):
        await _assert_hidden(client, person, workspace, private, task)


async def test_reinviting_supersedes_the_first_code(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    bob = await _signup(client, "Bob")
    body = {"email": bob.email, "role": "member"}
    capsys.readouterr()
    await client.post(f"/v1/workspaces/{workspace}/invites", headers=ada.h, json=body)
    first = await _console_code(capsys)
    await client.post(f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={**body, "role": "guest"})
    second = await _console_code(capsys)
    assert first != second
    assert (
        await client.post("/v1/workspaces/invites/accept", headers=bob.h, json={"code": first})
    ).status_code == 404
    ok = await client.post("/v1/workspaces/invites/accept", headers=bob.h, json={"code": second})
    assert ok.status_code == 200 and ok.json()["role"] == "guest"


async def test_concurrent_invites_leave_one_live(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    email = f"race{uuid.uuid4().hex[:8]}@example.com"
    rs = await asyncio.gather(
        *(
            client.post(f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": email})
            for _ in range(6)
        )
    )
    assert all(r.status_code == 201 for r in rs), [r.text for r in rs]
    async with session_scope() as s:
        live = await s.scalar(
            select(func.count())
            .select_from(WorkspaceInvite)
            .where(
                WorkspaceInvite.workspace_id == uuid.UUID(workspace),
                WorkspaceInvite.email == email,
                WorkspaceInvite.revoked_at.is_(None),
            )
        )
    assert live == 1


async def test_admin_revokes_an_invite_and_the_code_dies(
    client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    ada, bob = await _signup(client, "Ada"), await _signup(client, "Bob")
    workspace = await _team(client, ada)
    capsys.readouterr()
    made = (
        await client.post(f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": bob.email})
    ).json()
    code = await _console_code(capsys)
    assert (
        await client.delete(f"/v1/workspaces/{workspace}/invites/{made['id']}", headers=ada.h)
    ).status_code == 204
    assert (
        await client.delete(f"/v1/workspaces/{workspace}/invites/{made['id']}", headers=ada.h)
    ).status_code == 404
    assert (
        await client.post("/v1/workspaces/invites/accept", headers=bob.h, json={"code": code})
    ).status_code == 404


async def test_an_undeliverable_invite_is_unminted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid import mailer

    async def _fail(**_: object) -> bool:
        return False

    monkeypatch.setattr(mailer, "send", _fail)
    ada = await _signup(client, "Ada")
    workspace = await _team(client, ada)
    assert (
        await client.post(
            f"/v1/workspaces/{workspace}/invites", headers=ada.h, json={"email": "d@example.com"}
        )
    ).status_code == 201
    await auth_ops.flush_outbound_email()
    assert (await client.get(f"/v1/workspaces/{workspace}/invites", headers=ada.h)).json() == []


async def test_two_co_owners_leaving_at_once_never_orphan_the_workspace(client: AsyncClient) -> None:
    """Under a race: the owner-count guard is serialized on the workspace row, so of
    two simultaneous leaves exactly one wins and one owner remains."""
    ada, bea = await _signup(client, "Ada"), await _signup(client, "Bea")
    workspace = await _team(client, ada)
    await _join(workspace, bea, WorkspaceRole.OWNER)
    rs = await asyncio.gather(
        client.post(f"/v1/workspaces/{workspace}/members/me/leave", headers=ada.h),
        client.post(f"/v1/workspaces/{workspace}/members/me/leave", headers=bea.h),
    )
    assert sorted(r.status_code for r in rs) == [204, 409], [r.text for r in rs]
    async with session_scope() as s:
        assert await WorkspaceMember.owner_count(s, uuid.UUID(workspace)) == 1


async def test_a_transfer_racing_the_target_leaving_keeps_an_owner(client: AsyncClient) -> None:
    ada, bea = await _signup(client, "Ada"), await _signup(client, "Bea")
    workspace = await _team(client, ada)
    await _join(workspace, bea, WorkspaceRole.MEMBER)
    await asyncio.gather(
        client.post(f"/v1/workspaces/{workspace}/transfer", headers=ada.h, json={"actor_id": bea.id}),
        client.post(f"/v1/workspaces/{workspace}/members/me/leave", headers=bea.h),
    )
    async with session_scope() as s:
        assert await WorkspaceMember.owner_count(s, uuid.UUID(workspace)) >= 1


async def test_an_api_key_acts_only_in_its_own_workspace(client: AsyncClient) -> None:
    """An API key belongs to one workspace: on another workspace's routes it is a stranger (404),
    even when the person behind it administers that workspace. A login session still can."""
    ada = await _signup(client, "Ada")
    team = await _team(client, ada)
    made = await client.post("/v1/auth/api-keys", headers=ada.h, json={"name": "ci"})
    assert made.status_code == 201, made.text
    key = _h(made.json()["key"])  # minted in Ada's personal workspace, her active one

    assert (await client.get(f"/v1/workspaces/{ada.personal_workspace}", headers=key)).status_code == 200
    assert (await client.get(f"/v1/workspaces/{team}", headers=key)).status_code == 404
    assert (await client.get(f"/v1/workspaces/{team}/members", headers=key)).status_code == 404
    invite = await client.post(
        f"/v1/workspaces/{team}/invites", headers=key, json={"email": "mallory@example.com", "role": "admin"}
    )
    assert invite.status_code == 404
    assert (await client.get(f"/v1/workspaces/{team}", headers=ada.h)).status_code == 200
