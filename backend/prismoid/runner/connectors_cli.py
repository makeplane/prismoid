# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid connectors` - the surfaces a task can reach (ADR-020).

    prismoid connectors catalog                           # the known providers, from the API
    prismoid connectors connect notion                    # create + consent in one motion
    prismoid connectors add https://mcp.example.com/mcp   # a custom server, by URL
    prismoid connectors policy notion --set search=allow --set create_page=ask
    prismoid do "summarize my meeting notes" --connector notion

Thin client of the API - never the database. Names resolve through the SERVER's data
catalog (`GET /v1/connectors/catalog`) - the bundled `default_connectors.yaml` died when the
hosted catalog landed (one source of truth; the API and the web read the same list).
An OAuth sign-in finishes only in the browser that started it (the callback checks an HttpOnly
cookie the start set), and a terminal is not that browser: `connect`/`authorize` point at the web
app's Connectors page, where you are signed in, and poll the row until the callback lands.
"""

from __future__ import annotations

import time
from typing import Any

import typer

from prismoid.runner import client

connectors = typer.Typer(
    help="Connectors: external MCP servers your tasks can call. Authorize once, attach per task.",
    no_args_is_help=True,
)

POLL_S = 2.0
_FINISH_IN_WEB = (
    "{name} signs in through your browser. Finish it on Prismoid's Connectors page (Connect on {name}),\n"
    "in the browser where you are signed in to Prismoid - a sign-in only completes in the browser that\n"
    "started it."
)
POLL_MAX_S = 180.0  # a consent screen, generously


def _catalog(*, as_json: bool = False) -> dict[str, dict[str, Any]]:
    answer = client.request("GET", "/v1/connectors/catalog", as_json=as_json)
    return {str(e["slug"]): e for e in answer.get("items", [])}


def _resolve_url(ref: str) -> str:
    if "://" in ref:
        return ref
    entries = _catalog()
    entry = entries.get(ref.lower())
    if entry is None:
        known = ", ".join(sorted(entries)) or "none published"
        client.fail(f"{ref!r} is not a URL or a catalog name (known: {known})")
    return str(entry["server_url"])


def _find(ref: str, *, as_json: bool) -> dict[str, Any]:
    """A slug or an id → the row. Client-side match over the (small) list - the CLI never guesses."""
    page = client.request("GET", "/v1/connectors", params={"limit": 200}, as_json=as_json)
    for row in page.get("items", []):
        if row["slug"] == ref or row["id"] == ref:
            return row
    client.fail(f"no connector {ref!r} - `prismoid connectors list`")


def _row_line(r: dict[str, Any]) -> str:
    tools = len(r.get("tools") or [])
    err = f" - {r['last_error']}" if r.get("last_error") else ""
    return f"  {r['slug']:<16} {r['status']:<13} {tools:>3} tool(s)  {r['server_url']}{err}"


@connectors.command("list")
def connectors_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Every connector you own, with status and tool count."""
    page = client.request("GET", "/v1/connectors", params={"limit": 200}, as_json=json_out)
    rows = page.get("items", [])
    lines = [_row_line(r) for r in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no connectors - `prismoid connectors add URL`")


@connectors.command("catalog")
def connectors_catalog(json_out: bool = typer.Option(False, "--json")) -> None:
    """The known providers (server data): connect any of them by name."""
    entries = _catalog(as_json=json_out)
    lines = [
        f"  {e['slug']:<12} {e['auth']:<6} {e['category']:<20} {e['description']}" for e in entries.values()
    ]
    client.emit(list(entries.values()), json_out, human="\n".join(lines) or "the catalog is empty")


@connectors.command("connect")
def connectors_connect(
    ref: str = typer.Argument(..., help="A catalog name (see `connectors catalog`) or an MCP server URL."),
    name: str = typer.Option("", "--name", help="Display name; also derives the tool prefix."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", hidden=True, help="No effect; kept so scripts still parse."
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Poll until the callback lands."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """One motion: create the connector; an OAuth one then finishes in the web app."""
    body: dict[str, Any] = {"catalog_slug": ref.lower()} if "://" not in ref else {"server_url": ref}
    if name:
        body["name"] = name
    answer = client.request("POST", "/v1/connectors/connect", body=body, as_json=json_out)
    row, url = answer["connector"], answer.get("authorize_url")
    if not url:
        hint = f" - {row['last_error']}" if row.get("last_error") else ""
        client.emit(answer, json_out, human=_row_line(row) + hint)
        return
    client.emit(answer, json_out, human=_row_line(row) + "\n" + _FINISH_IN_WEB.format(name=row["name"]))
    if wait:
        _wait_for_live(row["id"], json_out=json_out)


@connectors.command("add")
def connectors_add(
    ref: str = typer.Argument(..., help="An MCP server URL, or a catalog name (see `connectors catalog`)."),
    name: str = typer.Option("", "--name", help="Display name; also derives the tool prefix."),
    bearer: str = typer.Option("", "--bearer", help="A static token, for servers that hand keys out."),
    client_id: str = typer.Option("", "--client-id", help="Pre-registered OAuth client (DCR-less servers)."),
    client_secret: str = typer.Option("", "--client-secret", help="Its secret, if it has one."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Add a connector. No-auth servers go live immediately; OAuth servers park until `authorize`."""
    body: dict[str, Any] = {"server_url": _resolve_url(ref)}
    if name:
        body["name"] = name
    elif "://" not in ref:
        body["name"] = ref  # `add notion` should be called notion, not its hostname's guess
    if bearer:
        body["bearer_token"] = bearer
    if client_id:
        body["client_id"] = client_id
    if client_secret:
        body["client_secret"] = client_secret
    row = client.request("POST", "/v1/connectors", body=body, as_json=json_out)
    hint = "" if row["status"] == "live" else f"\n  next: prismoid connectors authorize {row['slug']}"
    client.emit(row, json_out, human=_row_line(row) + hint)


@connectors.command("show")
def connectors_show(
    ref: str = typer.Argument(..., help="The connector's slug or id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """One connector: status, tools, instructions, the whole face."""
    row = _find(ref, as_json=json_out)
    tools = "\n".join(f"    {t['name']:<24} {t.get('description', '')}" for t in row.get("tools") or [])
    human = _row_line(row) + ("\n  tools:\n" + tools if tools else "")
    if row.get("instructions"):
        human += f"\n  instructions: {row['instructions'][:200]}"
    client.emit(row, json_out, human=human)


@connectors.command("authorize")
def connectors_authorize(
    ref: str = typer.Argument(..., help="The connector's slug or id."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", hidden=True, help="No effect; kept so scripts still parse."
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Poll until the callback lands."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Point at the web app, where the sign-in finishes, and wait for the grant to land. Only an
    OAuth connector still waiting for its sign-in has one to finish."""
    row = _find(ref, as_json=json_out)
    if row.get("auth_kind") != "oauth":
        client.fail(
            f"{row['name']} does not sign in with OAuth - there is nothing to authorize", as_json=json_out
        )
    if row["status"] == "live":
        client.fail(
            f"{row['name']} is already connected - to sign in again, use Connect on {row['name']} on Prismoid's"
            " Connectors page",
            as_json=json_out,
        )
    client.emit(row, json_out, human=_FINISH_IN_WEB.format(name=row["name"]))
    if wait:
        _wait_for_live(row["id"], json_out=json_out)


def _wait_for_live(connector_id: str, *, json_out: bool) -> None:
    """The shared poll: watch the row until the OAuth callback lands (or honestly fails). A failure
    counts only once a sign-in has been under way: the row waits as it was until the web starts one."""
    deadline = time.monotonic() + POLL_MAX_S
    started = False
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        row = client.request("GET", f"/v1/connectors/{connector_id}", as_json=True)
        if row["status"] == "live":
            client.emit(row, json_out, human=f"connected - {len(row.get('tools') or [])} tool(s)")
            return
        started = started or row["status"] == "authorizing"
        if row["status"] == "error" or (started and row["status"] in ("needs_reauth", "pending_auth")):
            client.fail(f"authorization did not complete ({row['status']}): {row.get('last_error') or ''}")
    client.fail(
        "timed out waiting for the callback - finish in the browser, then `connectors show`",
        code=client.EXIT_TIMEOUT,
    )


@connectors.command("probe")
def connectors_probe(
    ref: str = typer.Argument(..., help="The connector's slug or id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Re-run the probe - refreshes the tool inventory, or recovers an `error` row."""
    row = _find(ref, as_json=json_out)
    row = client.request("POST", f"/v1/connectors/{row['id']}/probe", body={}, as_json=json_out)
    client.emit(row, json_out, human=_row_line(row))


@connectors.command("policy")
def connectors_policy(
    ref: str = typer.Argument(..., help="The connector's slug or id."),
    set_: list[str] = typer.Option(
        [], "--set", help="tool=allow|ask|off, repeatable. No --set prints the effective table."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Per-tool permissions: unmapped tools default to allow on a connector you
    explicitly connected; auto-mounted write tools gate on approval. --set to override."""
    row = _find(ref, as_json=json_out)
    if set_:
        overrides = {
            t["name"]: t["policy"] for t in row.get("tools") or [] if t.get("policy_source") == "override"
        }
        for pair in set_:
            tool, sep, value = pair.partition("=")
            if not sep:
                client.fail(f"--set wants tool=policy, got {pair!r}")
            overrides[tool.strip()] = value.strip()
        row = client.request(
            "PATCH", f"/v1/connectors/{row['id']}/policies", body={"policies": overrides}, as_json=json_out
        )
    marks = {"allow": "allow", "ask": "ask  ", "off": "off  "}
    lines = [
        f"    {t['name']:<28} {marks.get(t['policy'], t['policy']):<6}"
        f" {'override' if t.get('policy_source') == 'override' else 'default '}"
        f" {'read-only' if t.get('read_only') else ''}"
        for t in row.get("tools") or []
    ]
    client.emit(row, json_out, human=_row_line(row) + ("\n" + "\n".join(lines) if lines else ""))


@connectors.command("disable")
def connectors_disable(
    ref: str = typer.Argument(..., help="The connector's slug or id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Switch a connector off without forgetting it - tasks drop it at the next mount."""
    row = _find(ref, as_json=json_out)
    row = client.request("POST", f"/v1/connectors/{row['id']}/disable", body={}, as_json=json_out)
    client.emit(row, json_out, human=_row_line(row))


@connectors.command("enable")
def connectors_enable(
    ref: str = typer.Argument(..., help="The connector's slug or id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Switch a disabled connector back on - the probe decides what it is now."""
    row = _find(ref, as_json=json_out)
    row = client.request("POST", f"/v1/connectors/{row['id']}/enable", body={}, as_json=json_out)
    client.emit(row, json_out, human=_row_line(row))


@connectors.command("remove")
def connectors_remove(
    ref: str = typer.Argument(..., help="The connector's slug or id."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Delete a connector. Its tokens die with it; tasks that named it drop it at the next mount."""
    row = _find(ref, as_json=json_out)
    if not yes:
        typer.confirm(
            f"delete connector {row['slug']!r} (its stored authorization dies with it)?", abort=True
        )
    client.request("DELETE", f"/v1/connectors/{row['id']}", as_json=json_out)
    client.emit({"deleted": row["slug"]}, json_out, human=f"deleted {row['slug']}")
