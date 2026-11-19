# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid projects invite-link | invite-links | revoke-link | join` against the real API.

The CLI is a thin HTTP client, so the test runs the REAL commands and only swaps the wire: the
CLI's blocking `httpx.request` is bridged onto an ASGI client on the test's event loop, and each
"home" is the bearer token that person's `~/.prismoid` would hold. Ada and Bob are two homes on
one API; nothing here calls an operation directly except the workspace-membership setup.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import logfire
import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from prismoid.api.app import create_app
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.runner import client as cli_client
from prismoid.runner.main import app as cli_app
from prismoid.schemas.ids import PROJECT_INVITE_PREFIX

pytestmark = pytest.mark.integration

_PASSWORD = "a-long-enough-password"
_DEAD = "this invite is invalid or has expired"


@pytest.fixture
async def api(silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """The app on an ASGI client, and the CLI's `httpx.request` bridged onto it."""
    loop = asyncio.get_running_loop()
    app = create_app()
    # The API and the CLI share this process's stdout here, and the CLI's `--json` output is
    # parsed from it: keep logfire's console exporter (its span lines) out of the way.
    logfire.configure(console=False, send_to_logfire=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:

        def bridge(method: str, url: str, **kw: Any) -> httpx.Response:
            path = url.removeprefix(cli_client.api_url())  # the CLI asks for {api_url}{path}
            fut = asyncio.run_coroutine_threadsafe(
                c.request(
                    method,
                    path,
                    json=kw.get("json"),
                    params=kw.get("params"),
                    headers=kw.get("headers"),
                ),
                loop,
            )
            return fut.result(timeout=30)

        monkeypatch.setattr(cli_client.httpx, "request", bridge)
        yield c
    await auth_ops.flush_outbound_email()


class Home:
    """One person's `prismoid` install: their token, their workspace, their id."""

    def __init__(self, token: str, me: dict[str, Any]) -> None:
        self.token = token
        self.id = str(me["id"])
        self.personal_workspace = str(me["workspace"]["id"])

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def cli(self, *args: str) -> Any:
        """Run `prismoid <args>` as this person. The CLI blocks, so it gets its own thread."""
        runner = CliRunner()
        return await asyncio.to_thread(
            runner.invoke, cli_app, list(args), env={"PRISMOID_API_KEY": self.token}
        )

    async def cli_json(self, *args: str) -> Any:
        r = await self.cli(*args, "--json")
        assert r.exit_code == 0, f"{args}: {r.exit_code} {r.output}"
        return json.loads(r.stdout)


async def _signup(api: AsyncClient, name: str) -> Home:
    body = {"email": f"cli{uuid.uuid4().hex[:12]}@example.com", "name": name, "password": _PASSWORD}
    r = await api.post("/v1/auth/signup", json=body)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    me = await api.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return Home(token, me.json())


async def _launch(api: AsyncClient) -> tuple[Home, str, str]:
    """Ada, her team workspace (her session in it), and a Project in it: (ada, workspace_id, project_id)."""
    ada = await _signup(api, "Ada")
    workspace = await api.post("/v1/workspaces", headers=ada.headers(), json={"name": "Acme"})
    assert workspace.status_code == 201, workspace.text
    workspace_id = str(workspace.json()["id"])
    switched = await api.put(
        "/v1/auth/session/workspace", headers=ada.headers(), json={"workspace_id": workspace_id}
    )
    assert switched.status_code == 200, switched.text
    project = await api.post("/v1/projects", headers=ada.headers(), json={"name": "Launch"})
    assert project.status_code == 201, project.text
    return ada, workspace_id, str(project.json()["id"])


async def _teammate(api: AsyncClient, name: str, workspace_id: str) -> Home:
    home = await _signup(api, name)
    async with session_scope() as s:
        await WorkspaceMember.add(s, uuid.UUID(workspace_id), uuid.UUID(home.id), WorkspaceRole.MEMBER)
        await s.commit()
    return home


async def test_a_link_minted_in_one_home_is_joined_from_another_once_and_revoking_kills_it(
    api: AsyncClient,
) -> None:
    ada, workspace_id, project_id = await _launch(api)
    bob = await _teammate(api, "Bob", workspace_id)
    cy = await _teammate(api, "Cy", workspace_id)

    minted = await ada.cli_json("projects", "invite-link", project_id)
    code = minted["code"]
    assert code.startswith(PROJECT_INVITE_PREFIX)
    assert (minted["role"], minted["uses"], minted["state"]) == ("editor", 0, "live")
    assert "max_uses" not in minted

    joined = await bob.cli_json("projects", "join", code)
    assert joined == {"project_id": project_id, "workspace_id": workspace_id, "role": "editor"}

    listed = await ada.cli_json("projects", "invite-links", project_id)
    (row,) = listed
    assert (row["id"], row["uses"], row["state"]) == (minted["id"], 1, "used_up")
    assert "code" not in row  # shown once, never again

    spent = await cy.cli("projects", "join", code, "--json")  # single-use: Bob's join spent it
    assert spent.exit_code == cli_client.EXIT_ERROR  # a 404 is an error, not a denial
    assert _DEAD in spent.stderr

    unused = await ada.cli_json("projects", "invite-link", project_id)
    revoked = await ada.cli_json("projects", "revoke-link", project_id, unused["id"])
    assert revoked == {"ok": True}
    assert (await ada.cli_json("projects", "invite-links", project_id))[0]["state"] == "revoked"

    dead = await cy.cli("projects", "join", unused["code"], "--json")
    assert dead.exit_code == cli_client.EXIT_ERROR
    assert _DEAD in dead.stderr


async def test_role_and_expiry_reach_the_link_and_the_human_output_shows_the_code(
    api: AsyncClient,
) -> None:
    ada, _, project_id = await _launch(api)

    out = await ada.cli("projects", "invite-link", project_id, "--role", "viewer", "--expires", "3")
    assert out.exit_code == 0, out.output
    (line,) = [ln for ln in out.stdout.splitlines() if ln.startswith(PROJECT_INVITE_PREFIX)]
    assert f"open /join/{line.strip()} in the app" in out.stdout
    assert "one person" in out.stdout

    (row,) = await ada.cli_json("projects", "invite-links", project_id)
    assert (row["role"], row["uses"], row["state"]) == ("viewer", 0, "live")


async def test_a_pasted_link_joins_like_the_bare_code(api: AsyncClient) -> None:
    ada, workspace_id, project_id = await _launch(api)
    bob = await _teammate(api, "Bob", workspace_id)
    code = (await ada.cli_json("projects", "invite-link", project_id, "--role", "viewer"))["code"]

    joined = await bob.cli_json("projects", "join", f"https://app.example.com/join/{code}")
    assert joined["role"] == "viewer"


async def test_only_the_owner_mints_lists_and_revokes_and_a_denial_exits_2(api: AsyncClient) -> None:
    ada, workspace_id, project_id = await _launch(api)
    bob = await _teammate(api, "Bob", workspace_id)
    minted = await ada.cli_json("projects", "invite-link", project_id)
    await bob.cli_json("projects", "join", minted["code"])  # an editor now, still not the owner

    for args in (
        ("projects", "invite-link", project_id),
        ("projects", "invite-links", project_id),
        ("projects", "revoke-link", project_id, minted["id"]),
    ):
        r = await bob.cli(*args, "--json")
        assert r.exit_code == cli_client.EXIT_DENIED, f"{args}: {r.exit_code} {r.output}"
    assert (await ada.cli_json("projects", "invite-links", project_id))[0][
        "state"
    ] == "used_up"  # not revoked
