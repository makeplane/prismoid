# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid admin` - operator territory: users, the task console, spend, health, the audit trail,
and the versioned price book. Every command is a thin typed call onto `/v1/admin/*`
(the CLI is an API client) - the same audited door the web admin screens use.

    prismoid admin users list [--q ana] [--status suspended]
    prismoid admin users suspend <id> --reason "runaway spend"
    prismoid admin tasks list --stuck            # the firefighting view
    prismoid admin tasks cancel <id> --reason "wedged"
    prismoid admin spend [--daily] [--by model|user]
    prismoid admin health                        # exit 1 on clogged/stuck/doctor findings - cron it
    prismoid admin audit
    prismoid admin prices put reprice.json       # install version N+1
    prismoid admin bootstrap --email ops@x.com   # ON THE BOX: the first platform admin

`bootstrap` is the one command here that is not an API client: no web path grants platform admin,
so it runs inside the API container against the database (the `prismoid models seed` shape) and makes
the first admin there.

Repricing is **data, not a deploy**: `put` writes a new version, and the very next call is priced by
it. It never touches a past `usage_events` row - each already carries the `price_version` that priced
it, so an old invoice stays priced by the book that was in force when it was billed.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import typer

from prismoid.runner import client

admin = typer.Typer(
    help="Operator territory: users · tasks · spend · health · audit · prices.", no_args_is_help=True
)
prices = typer.Typer(help="The versioned price book. Repricing is data, not a deploy.", no_args_is_help=True)
users = typer.Typer(help="The user population: observe, suspend, grant, revoke.", no_args_is_help=True)
tasks = typer.Typer(help="The cross-user task console: list, peek, cancel.", no_args_is_help=True)
admin.add_typer(prices, name="prices")
admin.add_typer(users, name="users")
admin.add_typer(tasks, name="tasks")


def _render(book: dict[str, Any]) -> str:
    if not book.get("prices"):
        return f"  price book v{book['version']}  (empty)"
    lines = [f"  price book v{book['version']}"]
    for ref, units in sorted(book["prices"].items()):
        priced = "  ".join(f"{u}={v}" for u, v in sorted(units.items()))
        lines.append(f"    {ref:<30} {priced}")
    return "\n".join(lines)


@prices.command("get")
def prices_get(
    version: int = typer.Option(0, "--version", help="A past version; default is the book in force."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show the price book - the version in force, or a past one with --version."""
    path = "/v1/admin/price-book" + (f"?version={version}" if version else "")
    book = client.request("GET", path, as_json=json_out)
    client.emit(book, json_out, human=_render(book))


@prices.command("put")
def prices_put(
    file: pathlib.Path = typer.Argument(..., help='JSON: {"provider:model": {"unit": "usd_per_unit"}}'),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Install a new price-book version. **Copy-forward** - name only the models you are repricing.

    Old `usage_events` keep their `price_version`: repricing never rewrites a past invoice."""
    raw = json.loads(file.read_text())
    overrides = raw.get("prices", raw) if isinstance(raw, dict) else raw
    book = client.request("PUT", "/v1/admin/price-book", body={"prices": overrides}, as_json=json_out)
    client.emit(book, json_out, human="repriced →\n" + _render(book))


# ── users ──────────────────────────────────────────────────────────────────────────────────────


@users.command("list")
def users_list(
    q: str = typer.Option("", "--q", help="Email or name substring."),
    status: str = typer.Option("", "--status", help="active | suspended"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Every human, newest first."""
    params: dict[str, Any] = {}
    if q:
        params["q"] = q
    if status:
        params["status"] = status
    page = client.request("GET", "/v1/admin/users", params=params, as_json=json_out)
    lines = [
        f"  {u['id']}  {u['email']:<34} {u['status']:<10}"
        + ("admin" if u["is_admin"] else "")
        + ("  (deleted)" if u["deleted"] else "")
        for u in page["items"]
    ]
    client.emit(page, json_out, human="\n".join(lines) or "  (no users)")


@users.command("show")
def users_show(id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """One user: status, live sessions/keys, task counts, spend."""
    u = client.request("GET", f"/v1/admin/users/{id}", as_json=json_out)
    tasks_line = "  ".join(f"{k}={v}" for k, v in sorted(u["task_counts"].items())) or "none"
    human = (
        f"  {u['email']}  ({u['name']})\n"
        f"    status={u['status']}  admin={u['is_admin']}  deleted={u['deleted']}\n"
        f"    sessions={u['live_sessions']}  api_keys={u['live_api_keys']}\n"
        f"    tasks: {tasks_line}\n"
        f"    spend: ${u['spend_mtd_usd']} MTD · ${u['spend_total_usd']} all-time"
    )
    client.emit(u, json_out, human=human)


@users.command("suspend")
def users_suspend(
    id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="Lands in the audit row."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The kill switch: status → suspended, every session and API key revoked."""
    u = client.request("POST", f"/v1/admin/users/{id}/suspend", body={"reason": reason}, as_json=json_out)
    client.emit(u, json_out, human=f"  suspended {u['email']}")


@users.command("unsuspend")
def users_unsuspend(id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    u = client.request("POST", f"/v1/admin/users/{id}/unsuspend", as_json=json_out)
    client.emit(u, json_out, human=f"  unsuspended {u['email']} (they must sign in again)")


@users.command("grant")
def users_grant(id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """Grant the admin scope."""
    u = client.request("POST", f"/v1/admin/users/{id}/grant-admin", as_json=json_out)
    client.emit(u, json_out, human=f"  {u['email']} is now an admin")


@users.command("revoke")
def users_revoke(id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """Revoke the admin scope (refused for the last admin)."""
    u = client.request("POST", f"/v1/admin/users/{id}/revoke-admin", as_json=json_out)
    client.emit(u, json_out, human=f"  {u['email']} is no longer an admin")


@users.command("revoke-sessions")
def users_revoke_sessions(
    id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    """Sign a user out everywhere (sessions + API keys)."""
    r = client.request("POST", f"/v1/admin/users/{id}/revoke-sessions", as_json=json_out)
    client.emit(
        r, json_out, human=f"  revoked {r['sessions_revoked']} session(s), {r['api_keys_revoked']} key(s)"
    )


# ── tasks ──────────────────────────────────────────────────────────────────────────────────────


def _task_line(t: dict[str, Any]) -> str:
    owner = (t.get("owner") or {}).get("email", "(unattributed)")
    line = f"  {t['id']}  {t['status']:<10} ${t['spend_usd']:<10} {owner:<30} {t['goal_line'][:50]}"
    if t.get("stuck_reason"):
        line += f"\n      ⚠ {t['stuck_reason']}"
    return line


@tasks.command("list")
def tasks_list(
    stuck: bool = typer.Option(False, "--stuck", help="Only tasks the stall predicate flags."),
    status: list[str] = typer.Option([], "--status", help="Repeatable: running, waiting, …"),
    user: str = typer.Option("", "--user", help="Creator's user id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The whole population, newest first - the operator's console."""
    params: dict[str, Any] = {}
    if stuck:
        params["stuck"] = "true"
    if status:
        params["status"] = status
    if user:
        params["user_id"] = user
    page = client.request("GET", "/v1/admin/tasks", params=params, as_json=json_out)
    lines = [_task_line(t) for t in page["items"]]
    client.emit(page, json_out, human="\n".join(lines) or "  (no tasks)")


@tasks.command("show")
def tasks_show(id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """One task through the admin door. The view is audited."""
    t = client.request("GET", f"/v1/admin/tasks/{id}", as_json=json_out)
    client.emit(t, json_out, human=_task_line(t))


@tasks.command("cancel")
def tasks_cancel(
    id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="Lands in the audit row."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Stop a runaway task now - the user cancel's exact machinery, audited."""
    t = client.request("POST", f"/v1/admin/tasks/{id}/cancel", body={"reason": reason}, as_json=json_out)
    client.emit(t, json_out, human=f"  cancelled {t['id']} ({t['status']})")


# ── spend · health · audit ─────────────────────────────────────────────────────────────────────


@admin.command("spend")
def spend(
    days: int = typer.Option(30, "--days"),
    daily: bool = typer.Option(False, "--daily", help="Day buckets instead of the summary."),
    by: str = typer.Option("", "--by", help="With --daily: model | user."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """What the fleet is spending - the ledger, aggregated."""
    if daily:
        params: dict[str, Any] = {"days": days}
        if by:
            params["group"] = by
        points = client.request("GET", "/v1/admin/spend/daily", params=params, as_json=json_out)
        lines = [
            f"  {p['day']}  ${p['cost_usd']:<14} {p['calls']:>5} calls"
            + (f"  {p['key']}" if p["key"] else "")
            for p in points
        ]
        client.emit(points, json_out, human="\n".join(lines) or "  (no spend)")
        return
    s = client.request("GET", "/v1/admin/spend/summary", params={"days": days}, as_json=json_out)
    lines = [
        f"  last {s['days']}d: ${s['total_usd']} over {s['calls']} calls · MTD ${s['mtd_usd']} · today ${s['today_usd']}"
    ]
    if s["zero_cost_calls"]:
        lines.append(f"  ⚠ {s['zero_cost_calls']} calls metered at $0 - check the price book")
    lines.append("  by model:")
    lines += [f"    {m['provider']}:{m['model']:<40} ${m['cost_usd']}  ({m['calls']})" for m in s["by_model"]]
    lines.append("  top spenders:")
    lines += [f"    {t['email']:<34} ${t['cost_usd']}  ({t['calls']})" for t in s["top_spenders"]]
    client.emit(s, json_out, human="\n".join(lines))


@admin.command("health")
def health(json_out: bool = typer.Option(False, "--json")) -> None:
    """Structured vitals. Exit 1 when anything needs a hand - cron-able."""
    # ONE request: /admin/health carries its own alerts, computed from the same reading.
    h = client.request("GET", "/v1/admin/health", as_json=json_out)
    alerts = h["alerts"]
    lines = [
        f"  migration {h['migration']} · worker {h['worker']['status']}"
        + (f" (beat {h['worker']['beat_age_s']}s ago)" if h["worker"]["beat_age_s"] is not None else "")
    ]
    for q in h["queues"]:
        flag = "  ⚠ CLOGGED" if q["clogged"] else ""
        lines.append(
            f"  queue {q['name']:<14} enqueued={q['enqueued']} running={q['running']} oldest={q['oldest_wait_s']}s{flag}"
        )
    if h["version_ghosts"]:
        lines.append(f"  ⚠ {h['version_ghosts']} version ghost(s) pending sweep")
    if h["stuck_tasks"]:
        lines.append(f"  ⚠ {h['stuck_tasks']} stuck task(s) - `prismoid admin tasks list --stuck`")
    for f in h["doctor"]:
        lines.append(f"  doctor: [{f['check']}] {f['ref']}")
    for a in alerts:
        lines.append(f"  {a['severity'].upper():<5} {a['code']} ×{a['count']} - {a['detail']}")
    client.emit(h, json_out, human="\n".join(lines))
    if any(a["severity"] == "error" for a in alerts):
        raise typer.Exit(1)


@admin.command("audit")
def audit(json_out: bool = typer.Option(False, "--json")) -> None:
    """The trail: who did what to what, newest first."""
    page = client.request("GET", "/v1/admin/audit", as_json=json_out)
    lines = [
        f"  {r['at'][:19]}  {(r['admin'] or {}).get('email', '?'):<30} {r['action']:<22} {r['subject_type']}:{r['subject_id']}"
        for r in page["items"]
    ]
    client.emit(page, json_out, human="\n".join(lines) or "  (no admin actions yet)")


@admin.command("bootstrap")
def bootstrap(
    email: str = typer.Option(..., "--email", help="The person who becomes a platform admin."),
    name: str = typer.Option("", "--name", help="Their display name, for a new account."),
    new_code: bool = typer.Option(
        False, "--new-code", help="Mint a fresh set-password code for an existing account."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Make EMAIL a platform admin - on the box (`docker compose ... exec api prismoid admin bootstrap`).

    An existing account gains the admin scope (running it again changes nothing). A new address
    gets its account now, plus a one-time code printed here and nowhere else: its owner pastes it
    into the app's reset-password page (or `prismoid reset-password`) to choose a password. The code
    expires like a reset code; `--new-code` mints another."""
    import asyncio

    from prismoid.db.session import session_scope
    from prismoid.errors import PrismoidError
    from prismoid.operations import admin_ops

    async def run() -> admin_ops.Bootstrapped:
        async with session_scope() as session:
            return await admin_ops.bootstrap_admin(session, email=email, name=name or None, new_code=new_code)

    try:
        done = asyncio.run(run())
    except PrismoidError as exc:
        client.fail(str(exc), as_json=json_out)
    verb = "created and made a platform admin" if done.created else "is a platform admin"
    human = f"  {done.user.email} {verb}"
    if done.code:
        human += (
            "\n\n  One-time set-password code (shown once, not stored in the clear):\n"
            f"\n    {done.code}\n"
            "\n  Open the app's reset-password page (or run `prismoid reset-password`), paste the code,"
            "\n  and choose a password. It expires like a reset code; `--new-code` mints another."
        )
    data = {"email": done.user.email, "created": done.created, "admin": True, "code": done.code}
    client.emit(data, json_out, human=human)
