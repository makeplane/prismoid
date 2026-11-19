# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid projects ...` - shared folders for tasks: members, budgets and join links."""

from __future__ import annotations

from typing import Any

import typer

from prismoid.runner import client
from prismoid.runner.cli.choices import MemberRole

projects = typer.Typer(help="Projects: shared folders for tasks.", no_args_is_help=True)


@projects.command("create")
def projects_create(
    name: str = typer.Argument(..., help="The project name."),
    budget_usd: float = typer.Option(0.0, "--budget-usd", help="A spending ceiling (owner-only)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Create a project. You are its owner."""
    body: dict[str, Any] = {"name": name}
    if budget_usd:
        body["budget_usd"] = budget_usd
    s = client.request("POST", "/v1/projects", body=body, as_json=json_out)
    client.emit(s, json_out, human=f"{s['id']}  {s['name']}  (owner)")


@projects.command("list")
def projects_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Your projects, newest first (the first page - the endpoint keyset-paginates)."""
    resp = client.request("GET", "/v1/projects", as_json=json_out)
    lines = [
        f"{r['id']}  {r['name']:<24} {r['role']:<7} {'$' + str(r['budget_usd']) if r['budget_usd'] else '-'}"
        for r in resp["items"]
    ]
    client.emit(resp, json_out, human="\n".join(lines) or "no projects")


@projects.command("show")
def projects_show(
    project_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    """A project and your role in it."""
    s = client.request("GET", f"/v1/projects/{project_id}", as_json=json_out)
    budget = f"${s['budget_usd']}" if s["budget_usd"] else "unlimited"
    client.emit(s, json_out, human=f"{s['name']}\n  role:   {s['role']}\n  budget: {budget}")


@projects.command("update")
def projects_update(
    project_id: str = typer.Argument(...),
    name: str = typer.Option("", "--name", help="Rename the project."),
    budget_usd: float = typer.Option(0.0, "--budget-usd", help="Set the spending ceiling."),
    clear_budget: bool = typer.Option(False, "--clear-budget", help="Remove the ceiling (unlimited)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Rename or re-budget a project. Owner only - a non-owner gets `not found`, not `forbidden`."""
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if budget_usd:
        body["budget_usd"] = budget_usd
    if clear_budget:
        body["clear_budget"] = True
    s = client.request("PATCH", f"/v1/projects/{project_id}", body=body, as_json=json_out)
    client.emit(s, json_out, human=f"updated {s['name']}")


@projects.command("usage")
def projects_usage(
    project_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    """A project's month-to-date spend against its budget."""
    u = client.request("GET", f"/v1/projects/{project_id}/usage", as_json=json_out)
    budget = f"${u['budget_usd']}" if u["budget_usd"] else "unlimited"
    remaining = f" · ${u['remaining_usd']} left" if u["remaining_usd"] else ""
    client.emit(u, json_out, human=f"  MTD: ${u['month_to_date_usd']} / {budget}{remaining}")


@projects.command("artifacts")
def projects_artifacts(
    project_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    """Everything the project's tasks have produced."""
    rows = client.request("GET", f"/v1/projects/{project_id}/artifacts", as_json=json_out)
    lines = [f"{a['id']}  {a.get('type', ''):<10} {a.get('title', '')}" for a in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no artifacts yet")


@projects.command("share")
def projects_share(
    project_id: str = typer.Argument(...),
    email: str = typer.Argument(..., help="The colleague to share with, by email."),
    role: MemberRole = typer.Option(MemberRole.VIEWER, "--role", help="What they may do."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Add a member (owner only). Re-running with a different role updates it."""
    m = client.request(
        "POST",
        f"/v1/projects/{project_id}/members",
        body={"email": email, "role": role.value},
        as_json=json_out,
    )
    client.emit(m, json_out, human=f"shared with {email} as {m['role']}")


@projects.command("members")
def projects_members(
    project_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    """Who is in a project, and their roles."""
    rows = client.request("GET", f"/v1/projects/{project_id}/members", as_json=json_out)
    lines = [f"{r['actor_id']}  {r['role']}" for r in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no members (just the owner)")


@projects.command("unshare")
def projects_unshare(
    project_id: str = typer.Argument(...),
    actor_id: str = typer.Argument(..., help="The member's id (from `projects members`)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Remove a member (owner only). Takes effect on their next request."""
    client.request("DELETE", f"/v1/projects/{project_id}/members/{actor_id}", as_json=json_out)
    client.emit({"ok": True}, json_out, human="removed")


def _invite_code(raw: str) -> str:
    """The code from a bare `si_…` or from a pasted `…/join/<code>` link (query and fragment dropped)."""
    tail = raw.strip().split("#", 1)[0].split("?", 1)[0]
    return tail.rstrip("/").rsplit("/", 1)[-1]


@projects.command("invite-link")
def projects_invite_link(
    project_id: str = typer.Argument(..., help="The project id."),
    role: MemberRole = typer.Option(MemberRole.EDITOR, "--role", help="What the person who joins may do."),
    expires: int | None = typer.Option(None, "--expires", help="Days until it expires (1-30; default 7)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mint a join link (owner only). It lets ONE person in, then it is dead. The code is shown
    ONCE - the server keeps only its hash."""
    body: dict[str, Any] = {"role": role.value}
    if expires is not None:
        body["expires_in_days"] = expires
    inv = client.request("POST", f"/v1/projects/{project_id}/invites", body=body, as_json=json_out)
    # The CLI knows no web URL (only the API's), so it names the route instead of printing a link.
    human = (
        f"{inv['code']}\n"
        f"  open /join/{inv['code']} in the app, or: prismoid projects join {inv['code']}\n"
        f"  {inv['role']} · lets one person in · expires {str(inv['expires_at'])[:10]}\n"
        "  shown once - copy it now"
    )
    client.emit(inv, json_out, human=human)


@projects.command("invite-links")
def projects_invite_links(
    project_id: str = typer.Argument(..., help="The project id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """A project's join links (owner only): which are still live, never the codes."""
    rows = client.request("GET", f"/v1/projects/{project_id}/invites", as_json=json_out)
    lines = [f"{r['id']}  {r['role']:<7} {r['state']:<9} expires {str(r['expires_at'])[:10]}" for r in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no invite links")


@projects.command("revoke-link")
def projects_revoke_link(
    project_id: str = typer.Argument(..., help="The project id."),
    invite_id: str = typer.Argument(..., help="The invite id (from `projects invite-links`)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Revoke a join link (owner only). People who already joined through it stay."""
    client.request("DELETE", f"/v1/projects/{project_id}/invites/{invite_id}", as_json=json_out)
    client.emit({"ok": True}, json_out, human="revoked")


@projects.command("join")
def projects_join(
    code: str = typer.Argument(..., help="The invite code, or the pasted /join/<code> link."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Join a project through a link. You must already belong to its workspace."""
    joined = client.request(
        "POST", "/v1/projects/invites/accept", body={"code": _invite_code(code)}, as_json=json_out
    )
    client.emit(joined, json_out, human=f"you're in project {joined['project_id']} as {joined['role']}")
