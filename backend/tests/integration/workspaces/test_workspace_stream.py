# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A removed member's OPEN stream ends (ADR-061) - over the real SSE route.

`test_workspace_door.py` proves the re-proof function flips to False when the membership goes;
`test_stream.py` proves the generator ends on the first failed proof. This proves the chain
end to end on the wire: a real `GET /v1/tasks/{id}/events` held open by a member of the task's
workspace, the membership deleted underneath it, and the response finishing on its own within
`stream_reauth_seconds + 2` - for a login session and for an API key, both opened with a stream
ticket, whose row proofs find no workspace left to act in.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import pytest
from httpx import AsyncClient

from prismoid.conf import settings
from prismoid.db.models import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope

pytestmark = pytest.mark.integration

_TASK_BODY = {"goal": "write a short memo about tenancy", "clarify": "never", "budget": {"usd": "1"}}


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("credential", ["session", "api_key"])
async def test_a_removed_members_open_stream_ends_within_one_reproof(
    client: AsyncClient, credential: str
) -> None:
    reauth_s = settings.api.stream_reauth_seconds  # the client fixture's short interval
    email = f"strm{uuid.uuid4().hex[:12]}@example.com"
    signup = await client.post(
        "/v1/auth/signup", json={"email": email, "name": "Ada", "password": "a-long-enough-password"}
    )
    assert signup.status_code == 201, signup.text
    session_token = str(signup.json()["token"])
    me = (await client.get("/v1/auth/me", headers=_h(session_token))).json()
    ada_id = uuid.UUID(me["id"])

    async with session_scope() as s:  # a second workspace, so a login session has somewhere to rebind
        team = await Workspace.create(s, name="Acme", kind=WorkspaceKind.TEAM, created_by=ada_id)
        await WorkspaceMember.add(s, team.id, ada_id, WorkspaceRole.MEMBER)
        await s.commit()
        team_id = team.id
    switched = await client.put(
        "/v1/auth/session/workspace", headers=_h(session_token), json={"workspace_id": str(team_id)}
    )
    assert switched.status_code == 200, switched.text
    token = session_token
    if credential == "api_key":
        minted = await client.post("/v1/auth/api-keys", headers=_h(session_token), json={"name": "watcher"})
        token = str(minted.json()["key"])
    created = await client.post("/v1/tasks", headers=_h(token), json=_TASK_BODY)
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]

    # The stream, as a browser's EventSource opens it (a ticket minted with the credential), following live.
    minted = await client.post(f"/v1/tasks/{task_id}/events/ticket", headers=_h(token))
    assert minted.status_code == 200, minted.text
    ticket = minted.json()["ticket"]
    stream = asyncio.create_task(client.get(f"/v1/tasks/{task_id}/events?since=0&ticket={ticket}"))
    try:
        done, _ = await asyncio.wait({stream}, timeout=reauth_s * 3)
        assert not done, "a member's stream must stay open across passing re-proofs"

        async with session_scope() as s:
            member = await WorkspaceMember.get(s, team_id, ada_id)
            assert member is not None
            await s.delete(member)
            await s.commit()
        removed_at = time.monotonic()

        # No client disconnect, no cancel: the response must FINISH on its own. wait_for is the guard.
        response = await asyncio.wait_for(stream, timeout=reauth_s + 2)
        elapsed = time.monotonic() - removed_at
    finally:
        if not stream.done():
            stream.cancel()  # a failed assertion must not leave the request hanging
            with contextlib.suppress(asyncio.CancelledError):
                await stream
    assert response.status_code == 200
    assert "task.created" in response.text, "the replayed record arrived before the severance"
    assert elapsed <= reauth_s + 2, f"severed after {elapsed:.2f}s"
