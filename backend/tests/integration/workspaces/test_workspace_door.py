# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The doors bind the workspace, end to end through HTTP.

Signup makes a personal workspace with its first grant and binds it; the bearer door binds the
session's active workspace (rebinding a stale one to the person's earliest remaining membership);
an API key never rebinds; a suspended workspace is refused; the routine fire token mints into the
ROUTINE's workspace whatever the owner's browser is switched to.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update

from prismoid.api.app import create_app
from prismoid.api.deps import credential_of
from prismoid.conf import settings
from prismoid.db.models import Workspace, WorkspaceInvite, WorkspaceKind, WorkspaceMember, WorkspaceRole
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.session import AuthSession
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.schemas.principal import Credential

pytestmark = pytest.mark.integration

_TASK_BODY = {"goal": "write a short memo about tenancy", "clarify": "never", "budget": {"usd": "1"}}
_PASSWORD = "a-long-enough-password"


@pytest.fixture
async def client(silent_dispatcher: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    await auth_ops.flush_outbound_email()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _held(token: str) -> tuple[Credential, uuid.UUID]:
    """What an open stream holds of the credential that opened it: the row and its actor, never the token."""
    async with session_scope() as s:
        principal = await (ApiKey if token.startswith("ck_") else AuthSession).resolve(s, token)
        assert principal is not None
        return credential_of(principal), principal.actor.id


async def _signup(client: AsyncClient, name: str) -> str:
    email = f"door{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post("/v1/auth/signup", json={"email": email, "name": name, "password": _PASSWORD})
    assert r.status_code == 201, r.text
    return str(r.json()["token"])


async def _me(client: AsyncClient, token: str) -> dict:  # pyright: ignore[reportMissingTypeArgument]
    r = await client.get("/v1/auth/me", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


async def _team_for(user_id: str, role: WorkspaceRole = WorkspaceRole.MEMBER) -> uuid.UUID:
    async with session_scope() as s:
        team = await Workspace.create(s, name="Acme", kind=WorkspaceKind.TEAM, created_by=uuid.UUID(user_id))
        await WorkspaceMember.add(s, team.id, uuid.UUID(user_id), role)
        await s.commit()
        return team.id


async def test_signup_creates_personal_workspace_and_binds(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    me = await _me(client, token)
    assert me["workspace"]["kind"] == "personal" and me["workspace"]["role"] == "owner"
    assert me["workspace"]["name"] == "Ada's workspace"
    workspaces = (await client.get("/v1/workspaces", headers=_h(token))).json()
    assert [o["id"] for o in workspaces] == [me["workspace"]["id"]]


async def test_task_created_through_the_door_is_stamped(client: AsyncClient) -> None:
    token = await _signup(client, "Ada")
    workspace_id = (await _me(client, token))["workspace"]["id"]
    r = await client.post("/v1/tasks", headers=_h(token), json=_TASK_BODY)
    assert r.status_code == 202, r.text
    async with session_scope() as s:
        task = await s.get(Task, uuid.UUID(r.json()["task_id"]))
        assert task is not None and str(task.workspace_id) == workspace_id


async def test_switch_requires_membership(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    bob = await _signup(client, "Bob")
    bob_workspace = (await _me(client, bob))["workspace"]["id"]
    r = await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": bob_workspace})
    assert r.status_code == 404


async def test_switch_moves_the_session_and_an_api_key_cannot_switch(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    r = await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    assert r.status_code == 200, r.text
    assert r.json()["workspace"]["id"] == str(team_id) and r.json()["workspace"]["role"] == "member"
    assert (await _me(client, ada))["workspace"]["id"] == str(team_id), "the switch is on the session row"

    key = (await client.post("/v1/auth/api-keys", headers=_h(ada), json={"name": "ci"})).json()
    assert key["workspace_id"] == str(team_id), "a key belongs to the workspace it was minted in"
    refused = await client.put(
        "/v1/auth/session/workspace", headers=_h(key["key"]), json={"workspace_id": ada_me["workspace"]["id"]}
    )
    assert refused.status_code == 400


async def test_revoked_membership_rebinds_to_remaining_workspace(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    r = await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    assert r.status_code == 200
    async with session_scope() as s:
        member = await WorkspaceMember.get(s, team_id, uuid.UUID(ada_me["id"]))
        assert member is not None
        await s.delete(member)
        await s.commit()
    me = await _me(client, ada)
    assert (
        me["workspace"]["id"] == ada_me["workspace"]["id"]
    )  # back in the personal workspace, not a 500, not a leak
    workspaces = (await client.get("/v1/workspaces", headers=_h(ada))).json()
    assert [o["id"] for o in workspaces] == [ada_me["workspace"]["id"]]


async def test_api_key_for_a_left_workspace_is_401(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    key = (await client.post("/v1/auth/api-keys", headers=_h(ada), json={"name": "ci"})).json()["key"]
    assert (await client.get("/v1/tasks", headers=_h(key))).status_code == 200
    async with session_scope() as s:
        member = await WorkspaceMember.get(s, team_id, uuid.UUID(ada_me["id"]))
        assert member is not None
        await s.delete(member)
        await s.commit()
    assert (await client.get("/v1/tasks", headers=_h(key))).status_code == 401, "a key never rebinds"


async def _strand(client: AsyncClient, token: str) -> None:
    """Suspend the person's only workspace: every membership is gone or suspended."""
    personal = (await _me(client, token))["workspace"]["id"]
    async with session_scope() as s:
        await s.execute(
            update(Workspace).where(Workspace.id == uuid.UUID(personal)).values(status="suspended")
        )
        await s.commit()


async def test_suspended_workspace_is_refused_at_the_door(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    await _strand(client, ada)
    r = await client.get("/v1/tasks", headers=_h(ada))
    assert r.status_code == 403, "no active workspace left - the door answers 403"
    assert r.json()["error"]["message"] == "no active workspace"


async def test_a_person_with_no_workspace_left_can_found_one(client: AsyncClient) -> None:
    """The way back in: a person whose every membership is gone still reads `me` (workspace null),
    lists their workspaces and creates one - and the next request acts in it."""
    ada = await _signup(client, "Ada")
    await _strand(client, ada)
    me = await client.get("/v1/auth/me", headers=_h(ada))
    assert me.status_code == 200, me.text
    assert me.json()["workspace"] is None
    assert (await client.get("/v1/workspaces", headers=_h(ada))).json() == []
    created = await client.post("/v1/workspaces", headers=_h(ada), json={"name": "Fresh Start"})
    assert created.status_code == 201, created.text
    assert (await _me(client, ada))["workspace"]["id"] == created.json()["id"], "the session rebinds into it"
    assert (await client.get("/v1/tasks", headers=_h(ada))).status_code == 200


async def test_a_stream_severs_once_its_session_has_no_workspace_left(client: AsyncClient) -> None:
    """The way back in is for the door's five routes only: a live stream re-proves `require_workspace`."""
    from prismoid.api.stream import _still_authorized  # pyright: ignore[reportPrivateUsage]

    ada = await _signup(client, "Ada")
    task_id = uuid.UUID((await client.post("/v1/tasks", headers=_h(ada), json=_TASK_BODY)).json()["task_id"])
    held = await _held(ada)
    assert await _still_authorized(*held, task_id) is True
    await _strand(client, ada)
    assert await _still_authorized(*held, task_id) is False


async def test_a_person_with_no_workspace_left_can_accept_an_invite_and_log_out(
    client: AsyncClient,
) -> None:
    ada_me = await _me(client, await _signup(client, "Ada"))
    team_id = await _team_for(ada_me["id"], WorkspaceRole.OWNER)
    bob_email = f"door{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post("/v1/auth/signup", json={"email": bob_email, "name": "Bob", "password": _PASSWORD})
    bob = str(r.json()["token"])
    await _strand(client, bob)
    async with session_scope() as s:
        _, code = await WorkspaceInvite.mint(
            s, team_id, bob_email, WorkspaceRole.MEMBER, uuid.UUID(ada_me["id"])
        )
        await s.commit()
    joined = await client.post("/v1/workspaces/invites/accept", headers=_h(bob), json={"code": code})
    assert joined.status_code == 200, joined.text
    assert (await _me(client, bob))["workspace"]["id"] == str(team_id)

    carl = await _signup(client, "Carl")
    await _strand(client, carl)
    assert (await client.post("/v1/auth/logout", headers=_h(carl))).status_code == 200
    assert (await client.get("/v1/auth/me", headers=_h(carl))).status_code == 401


async def test_routine_fire_token_mints_in_the_routines_workspace(client: AsyncClient) -> None:
    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    created = await client.post(
        "/v1/routines",
        headers=_h(ada),
        json={
            "name": f"standup-{uuid.uuid4().hex[:6]}",
            "instruction": "post the standup summary to the thread",
            "schedule": {"cron": "0 9 * * 1-5", "timezone": "UTC"},
            "budget_usd": 1,
        },
    )
    assert created.status_code == 201, created.text
    routine_id = created.json()["id"]
    minted = await client.post(f"/v1/routines/{routine_id}/token", headers=_h(ada))
    assert minted.status_code == 201, minted.text
    fire_token = minted.json()["token"]

    back = await client.put(
        "/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": ada_me["workspace"]["id"]}
    )
    assert back.status_code == 200 and back.json()["workspace"]["id"] == ada_me["workspace"]["id"]

    fired = await client.post(f"/v1/routines/{routine_id}/fire", headers=_h(fire_token), json={})
    assert fired.status_code == 202, fired.text
    assert fired.json()["decision"] == "minted", fired.json()
    async with session_scope() as s:
        task = await s.get(Task, uuid.UUID(fired.json()["task_id"]))
        assert task is not None and task.workspace_id == team_id, "the fire lands in the routine's workspace"


async def test_login_opens_in_the_earliest_membership(client: AsyncClient) -> None:
    email = f"door{uuid.uuid4().hex[:12]}@example.com"
    await client.post("/v1/auth/signup", json={"email": email, "name": "Ada", "password": _PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    me = await _me(client, login.json()["token"])
    assert me["workspace"]["kind"] == "personal" and me["workspace"]["role"] == "owner"


async def test_the_stream_reproof_severs_a_key_whose_member_left(client: AsyncClient) -> None:
    """The live stream re-proves through the door's own body: the membership, not
    just the credential - a removed member's open stream severs within one interval."""
    from prismoid.api.stream import _still_authorized  # pyright: ignore[reportPrivateUsage]

    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    key = (await client.post("/v1/auth/api-keys", headers=_h(ada), json={"name": "ci"})).json()["key"]
    task_id = uuid.UUID((await client.post("/v1/tasks", headers=_h(key), json=_TASK_BODY)).json()["task_id"])
    held = await _held(key)
    assert await _still_authorized(*held, task_id) is True
    async with session_scope() as s:
        member = await WorkspaceMember.get(s, team_id, uuid.UUID(ada_me["id"]))
        assert member is not None
        await s.delete(member)
        await s.commit()
    assert await _still_authorized(*held, task_id) is False


async def test_a_job_scope_acts_for_its_rows_workspace(client: AsyncClient) -> None:
    """The worker's door: `row_scope` binds the row's workspace before the job's work."""
    from prismoid.db.models import tenant
    from prismoid.db.session import row_scope

    ada = await _signup(client, "Ada")
    workspace_id = uuid.UUID((await _me(client, ada))["workspace"]["id"])
    task_id = uuid.UUID((await client.post("/v1/tasks", headers=_h(ada), json=_TASK_BODY)).json()["task_id"])
    async with row_scope(Task, task_id) as s:
        assert tenant.bound_workspace(s) == workspace_id


async def test_the_stream_reproof_severs_a_session_whose_member_left(client: AsyncClient) -> None:
    """A LOGIN SESSION rebinds at the door when its workspace membership goes - so the
    re-proof must also fence the subject's workspace, or a removed member's task and conversation
    streams keep flowing from their personal workspace."""
    from prismoid.api.stream import (
        _still_authorized,  # pyright: ignore[reportPrivateUsage]
        _still_authorized_conversation,  # pyright: ignore[reportPrivateUsage]
    )
    from prismoid.db.models import tenant
    from prismoid.db.models.actor import Actor
    from prismoid.operations import bots as bot_ops
    from prismoid.operations import chat as chat_ops
    from prismoid.schemas.bots import BotSource

    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    task_id = uuid.UUID((await client.post("/v1/tasks", headers=_h(ada), json=_TASK_BODY)).json()["task_id"])
    async with session_scope(team_id) as s:
        actor = await s.get(Actor, uuid.UUID(ada_me["id"]))
        assert actor is not None
        tenant.bind(s, team_id, WorkspaceRole.MEMBER.value)
        name = f"scribe-{uuid.uuid4().hex[:8]}"
        bot_md = (
            f"---\nname: {name}\ndescription: >\n  Writes release notes. Load when: a release needs"
            " notes. Not for: contracts.\ncapability: fast-executor\nproduces: document\n---\nWrite plainly.\n"
        )
        await bot_ops.push(s, actor=actor, name=name, source=BotSource(bot_md=bot_md))
        conv_id = (await chat_ops.open_conversation(s, actor, bot_name=name)).id
        await s.commit()

    held = await _held(ada)
    assert await _still_authorized(*held, task_id) is True
    assert await _still_authorized_conversation(*held, conv_id) is True
    async with session_scope() as s:
        member = await WorkspaceMember.get(s, team_id, uuid.UUID(ada_me["id"]))
        assert member is not None
        await s.delete(member)
        await s.commit()
    assert (await _me(client, ada))["workspace"]["id"] == ada_me["workspace"]["id"], "the session rebound"
    assert await _still_authorized(*held, task_id) is False, "the team task's stream severs"
    assert await _still_authorized_conversation(*held, conv_id) is False, "and the conversation's"


async def test_an_admin_pause_writes_the_inbox_item_in_the_routines_workspace(client: AsyncClient) -> None:
    """The console is platform, but a write on ONE tenant row acts for that row's workspace."""
    from sqlalchemy import select

    from prismoid.db.models.actor import Actor, Scope
    from prismoid.db.models.inbox import InboxItem

    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    created = await client.post(
        "/v1/routines",
        headers=_h(ada),
        json={
            "name": f"standup-{uuid.uuid4().hex[:6]}",
            "instruction": "post the standup summary to the thread",
            "schedule": {"cron": "0 9 * * 1-5", "timezone": "UTC"},
            "budget_usd": 1,
        },
    )
    routine_id = uuid.UUID(created.json()["id"])

    operator = await _signup(client, "Op")
    async with session_scope() as s:
        admin = await s.get(Actor, uuid.UUID((await _me(client, operator))["id"]))
        assert admin is not None
        admin.scopes = [*admin.scopes, Scope.ADMIN.value]
        await s.commit()
    r = await client.post(
        f"/v1/admin/routines/{routine_id}/pause", headers=_h(operator), json={"reason": "runaway spend"}
    )
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        workspaces = list(
            await s.scalars(
                select(InboxItem.workspace_id).where(InboxItem.actor_id == uuid.UUID(ada_me["id"]))
            )
        )
    assert workspaces and all(o == team_id for o in workspaces), workspaces


async def _an_email_channel(workspace_id: uuid.UUID, owner: dict[str, Any], bot_name: str) -> dict[str, str]:
    """An email channel made before the kind was refused at create: the row a deployment may still hold."""
    from prismoid.db.models import tenant
    from prismoid.db.models.bot_channel import BotChannel

    async with session_scope() as s:
        tenant.bind(s, workspace_id)
        row = BotChannel(
            owner_actor_id=uuid.UUID(owner["id"]),
            bot_name=bot_name,
            kind="email",
            address=f"{bot_name}-{uuid.uuid4().hex[:6]}@bots.example.test",
            config={"allowlist": [str(owner["email"]).lower()]},
        )
        s.add(row)
        await s.commit()
        return {"id": str(row.id), "address": str(row.address)}


async def test_the_email_inbound_webhook_lands_in_the_channels_workspace(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth exception #7: the operator's shared header secret is the whole
    auth and the recipient ADDRESS resolves the channel - the line lands in the CHANNEL's workspace
    (made while its owner acted in her team; she is back in her personal workspace when mail arrives),
    and no caller credential is ever read. Without the secret, a real address in another workspace
    answers exactly like an address that does not exist: the door is no existence oracle."""
    from pydantic import SecretStr

    from prismoid.db.models.conversation import Conversation
    from prismoid.db.models.message import Message

    monkeypatch.setattr(settings.channels, "kinds", "webhook,email")
    monkeypatch.setattr(settings.channels, "email_domain", "bots.example.test")
    monkeypatch.setattr(settings.channels, "email_inbound_secret", SecretStr("operator-secret"))

    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    name = f"scribe-{uuid.uuid4().hex[:8]}"
    bot_md = (
        f"---\nname: {name}\ndescription: >\n  Writes release notes. Load when: a release needs"
        " notes. Not for: contracts.\ncapability: fast-executor\nproduces: document\n---\nWrite plainly.\n"
    )
    made = await client.post("/v1/bots", headers=_h(ada), json={"name": name, "bot_md": bot_md})
    assert made.status_code == 201, made.text
    refused = await client.post(f"/v1/bots/{name}/channels", headers=_h(ada), json={"kind": "email"})
    assert refused.status_code == 422, "the email kind is refused at create until mail is authenticated"
    address = (await _an_email_channel(team_id, ada_me, name))["address"]
    await client.put(
        "/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": ada_me["workspace"]["id"]}
    )

    mail = {"to": address, "from": ada_me["email"], "subject": "notes", "text": "draft the 3.2 notes"}
    landed = await client.post(
        "/v1/channels/inbound/email", headers={"X-Prismoid-Channel-Secret": "operator-secret"}, json=mail
    )
    assert landed.status_code == 202, landed.text
    assert landed.json()["status"] == "refused" and landed.json()["task_id"] is None
    assert "sender-authentication" in landed.json()["detail"], "an existing email channel refuses honestly"
    async with session_scope() as s:
        conv = await s.get(Conversation, uuid.UUID(landed.json()["conversation_id"]))
        assert conv is not None and conv.workspace_id == team_id, "the line lands in the channel's workspace"
        line = await s.get(Message, (conv.id, landed.json()["seq"]))
        assert line is not None and "3.2 notes" in line.text and line.verdict == "refused"

    # A stranger from another workspace, holding a live credential of their own, without the secret.
    bob = await _signup(client, "Bob")
    absent = f"nobody-{uuid.uuid4().hex[:6]}@bots.example.test"

    def masked(r: Response) -> tuple[int, object]:
        body = r.json()
        body["error"].pop("request_id", None)  # the per-request correlation id differs by design
        return r.status_code, body

    guesses = [
        await client.post(
            "/v1/channels/inbound/email",
            headers={**_h(bob), "X-Prismoid-Channel-Secret": "a-guess"},
            json={**mail, "to": to, "from": "bob@example.com"},
        )
        for to in (address, absent)
    ]
    assert masked(guesses[0]) == masked(guesses[1]) and guesses[0].status_code == 401, [
        g.text for g in guesses
    ]
    # With the secret, an address that resolves nothing is a plain 404 - never a guess at a workspace.
    missing = await client.post(
        "/v1/channels/inbound/email",
        headers={"X-Prismoid-Channel-Secret": "operator-secret"},
        json={**mail, "to": absent},
    )
    assert missing.status_code == 404, missing.text


async def test_a_removed_members_channels_close_in_the_workspace_that_removed_them(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routine SKIPPED_MEMBERSHIP rule, for channels: once the
    owner is removed from the channel's workspace, the bc_ pair answers exactly like a wrong token
    (no oracle), inbound mail is a refused row wearing why and mints nothing, and the channel
    wears BROKEN so the minute drains stop offering it."""
    from pydantic import SecretStr
    from sqlalchemy import func, select

    from prismoid.db.models.bot_channel import BotChannel
    from prismoid.db.models.message import Message

    monkeypatch.setattr(settings.channels, "kinds", "webhook,email")
    monkeypatch.setattr(settings.channels, "email_domain", "bots.example.test")
    monkeypatch.setattr(settings.channels, "email_inbound_secret", SecretStr("operator-secret"))

    ada = await _signup(client, "Ada")
    ada_me = await _me(client, ada)
    team_id = await _team_for(ada_me["id"])
    await client.put("/v1/auth/session/workspace", headers=_h(ada), json={"workspace_id": str(team_id)})
    name = f"scribe-{uuid.uuid4().hex[:8]}"
    bot_md = (
        f"---\nname: {name}\ndescription: >\n  Writes release notes. Load when: a release needs"
        " notes. Not for: contracts.\ncapability: fast-executor\nproduces: document\n---\nWrite plainly.\n"
    )
    assert (
        await client.post("/v1/bots", headers=_h(ada), json={"name": name, "bot_md": bot_md})
    ).status_code == 201
    hook = (await client.post(f"/v1/bots/{name}/channels", headers=_h(ada), json={"kind": "webhook"})).json()
    mail_door = await _an_email_channel(team_id, ada_me, name)
    path = f"/v1/channels/{hook['id']}/messages"
    assert (await client.get(path, headers=_h(hook["token"]))).status_code == 200, "open before removal"

    async with session_scope() as s:
        await WorkspaceMember.remove(s, team_id, uuid.UUID(ada_me["id"]))
        await s.commit()

    def masked(r: Response) -> tuple[int, object]:
        body = r.json()
        body["error"].pop("request_id", None)
        return r.status_code, body

    line = {"message": "draft the 3.3 notes"}
    for method in ("POST", "GET"):
        kw = {"json": line} if method == "POST" else {}
        real = await client.request(method, path, headers=_h(hook["token"]), **kw)  # pyright: ignore[reportArgumentType]
        fake = await client.request(method, path, headers=_h("bc_" + "x" * 40), **kw)  # pyright: ignore[reportArgumentType]
        assert real.status_code == 401 and masked(real) == masked(fake), (method, real.text, fake.text)

    mail = {
        "to": mail_door["address"],
        "from": ada_me["email"],
        "subject": "notes",
        "text": "draft the notes",
    }
    landed = await client.post(
        "/v1/channels/inbound/email", headers={"X-Prismoid-Channel-Secret": "operator-secret"}, json=mail
    )
    assert landed.status_code == 202, landed.text
    assert landed.json()["status"] == "refused" and landed.json()["task_id"] is None
    assert "no longer a member" in landed.json()["detail"]
    async with session_scope() as s:
        row = await s.get(Message, (uuid.UUID(landed.json()["conversation_id"]), landed.json()["seq"]))
        assert (
            row is not None
            and row.verdict == "refused"
            and "no longer a member" in (row.verdict_reason or "")
        )
        for cid in (hook["id"], mail_door["id"]):
            channel = await s.get(BotChannel, uuid.UUID(cid))
            assert channel is not None and channel.status == "broken", "the drains stop offering it"
        minted = await s.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.workspace_id == team_id, Task.creator_actor_id == uuid.UUID(ada_me["id"]))
        )
        assert minted == 0


async def test_the_telegram_drain_refuses_and_then_skips_a_removed_members_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The telegram drain walks the one inbound path: after the owner's removal a polled line is
    a refused row wearing why (nothing minted, nothing replied), the channel wears BROKEN, and
    the sweep's roster (`all_active`) stops offering it - the next minute's drain skips it."""
    import httpx

    from prismoid.db.models.bot_channel import BotChannel
    from prismoid.db.models.message import Message
    from prismoid.operations import channels as channel_ops
    from prismoid.operations import cipher
    from tests.support.cast import member

    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 7,
                "message": {"text": "start the report", "chat": {"id": 42}, "from": {"username": "ada"}},
            }
        ],
    }
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=updates))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    async with session_scope() as s:
        owner, workspace = await member(s, "Ada")
        row = BotChannel(
            id=uuid.uuid4(),
            owner_actor_id=owner.id,
            bot_name="scribe",
            kind="telegram",
            label="tg",
            address=f"@tg{uuid.uuid4().hex[:6]}",
            config={"telegram_offset": 0, "allowlist": ["@ada"]},
            config_enc=cipher.encrypt("123456:ABC"),
            key_id=cipher.key_id(),
            budget_usd_per_turn=0.5,
            daily_message_cap=50,
        )
        s.add(row)
        await WorkspaceMember.remove(s, workspace.id, owner.id)
        await s.commit()

        await channel_ops.telegram_drain(s, row)  # counts lines walked, refused ones included
        refused = await s.scalar(select(Message).where(Message.provider_message_id == "tg-7"))
        assert refused is not None and refused.verdict == "refused" and refused.task_id is None
        assert "no longer a member" in refused.verdict_reason
        fresh = await s.get(BotChannel, row.id)
        assert fresh is not None and fresh.status == "broken"
        assert row.id not in {c.id for c in await BotChannel.all_active(s)}, "the next drain skips it"
