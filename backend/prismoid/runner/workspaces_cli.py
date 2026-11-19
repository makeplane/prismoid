# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid workspaces` - the workspace, as a command (ADR-061).

    prismoid workspaces list · create NAME · switch SLUG     # switch needs a login session, not an API key
    prismoid workspaces members · invite EMAIL --role member · role EMAIL ROLE · remove EMAIL

Slugs resolve to ids through `GET /v1/workspaces`; emails resolve to actor ids through the ACTIVE
workspace's member list (`/v1/auth/me` names it). Not-found is exit 1, like every other error.
"""

from __future__ import annotations

from typing import Any

import typer

from prismoid.runner import client

workspaces = typer.Typer(help="Workspaces: the tenant you work in.", no_args_is_help=True)
_JSON = typer.Option(False, "--json")


def _active_workspace_id(json_out: bool) -> str:
    workspace = client.request("GET", "/v1/auth/me", as_json=json_out)["workspace"]
    if workspace is None:  # every membership is gone - refused as the door refuses
        client.fail(
            "no active workspace - `prismoid workspaces create NAME`, or join with an invite code on the web",
            client.EXIT_DENIED,
            json_out,
        )
    return str(workspace["id"])


def _member_id(workspace_id: str, email: str, json_out: bool) -> str:
    rows = client.request("GET", f"/v1/workspaces/{workspace_id}/members", as_json=json_out)
    for row in rows:
        if str(row["user"]["email"]).lower() == email.strip().lower():
            return str(row["user"]["id"])
    client.fail(f"no member {email} in the active workspace", client.EXIT_ERROR, json_out)


def _workspace_line(o: dict[str, Any]) -> str:
    return f"{o['slug']:<24} {o['name']:<24} {o['kind']:<9} {o['role']}"


@workspaces.command("list")
def workspaces_list(json_out: bool = _JSON) -> None:
    """Every workspace you belong to, with your role."""
    rows = client.request("GET", "/v1/workspaces", as_json=json_out)
    client.emit(rows, json_out, human="\n".join(_workspace_line(o) for o in rows) or "no workspaces")


@workspaces.command("create")
def workspaces_create(name: str = typer.Argument(...), json_out: bool = _JSON) -> None:
    """Create a team workspace; you own it."""
    workspace = client.request("POST", "/v1/workspaces", body={"name": name}, as_json=json_out)
    client.emit(workspace, json_out, human=_workspace_line(workspace))


@workspaces.command("switch")
def workspaces_switch(slug: str = typer.Argument(...), json_out: bool = _JSON) -> None:
    """Make a workspace this login session's active one (an API key is pinned to its workspace)."""
    rows = client.request("GET", "/v1/workspaces", as_json=json_out)
    match = next((o for o in rows if o["slug"] == slug), None)
    if match is None:
        client.fail(
            f"no workspace with slug {slug!r} - see `prismoid workspaces list`", client.EXIT_ERROR, json_out
        )
    client.request("PUT", "/v1/auth/session/workspace", body={"workspace_id": match["id"]}, as_json=json_out)
    client.emit(match, json_out, human=f"now working in {match['name']} ({match['slug']})")


@workspaces.command("members")
def workspaces_members(json_out: bool = _JSON) -> None:
    """The active workspace's members."""
    rows = client.request("GET", f"/v1/workspaces/{_active_workspace_id(json_out)}/members", as_json=json_out)
    lines = [f"{r['user']['email']:<32} {r['role']:<7} {r['user']['name']}" for r in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no members")


@workspaces.command("invite")
def workspaces_invite(
    email: str = typer.Argument(...),
    role: str = typer.Option("member", "--role", help="member | admin | guest"),
    json_out: bool = _JSON,
) -> None:
    """Invite someone to the active workspace by email."""
    if role not in ("member", "admin", "guest"):
        client.fail("--role must be member, admin or guest", client.EXIT_ERROR, json_out)
    workspace_id = _active_workspace_id(json_out)
    inv = client.request(
        "POST",
        f"/v1/workspaces/{workspace_id}/invites",
        body={"email": email, "role": role},
        as_json=json_out,
    )
    client.emit(inv, json_out, human=f"invited {email} as {role}")


@workspaces.command("role")
def workspaces_role(
    email: str = typer.Argument(...),
    role: str = typer.Argument(..., help="owner | admin | member | guest"),
    json_out: bool = _JSON,
) -> None:
    """Change a member's role in the active workspace."""
    workspace_id = _active_workspace_id(json_out)
    actor_id = _member_id(workspace_id, email, json_out)
    out = client.request(
        "PATCH", f"/v1/workspaces/{workspace_id}/members/{actor_id}", body={"role": role}, as_json=json_out
    )
    client.emit(out or {"ok": True}, json_out, human=f"{email} is now {role}")


@workspaces.command("remove")
def workspaces_remove(email: str = typer.Argument(...), json_out: bool = _JSON) -> None:
    """Remove a member from the active workspace."""
    workspace_id = _active_workspace_id(json_out)
    actor_id = _member_id(workspace_id, email, json_out)
    client.request("DELETE", f"/v1/workspaces/{workspace_id}/members/{actor_id}", as_json=json_out)
    client.emit({"ok": True}, json_out, human=f"removed {email}")
