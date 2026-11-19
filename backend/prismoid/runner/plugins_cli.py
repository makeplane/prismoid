# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid plugins` - Agent Plugins v1 bundles, in and out (ADR-045).

    prismoid plugins install ./some-plugin      # skills + bots → drafts, MCP → connectors
    prismoid plugins export contract-reviewer   # one bot → a portable bundle
    prismoid plugins list                       # the receipts

`install` and `export` are operator commands (the thin-client exception, like `skills seed`):
they read and write local directories the API server has no path to, so they open the database
directly.
`list` is the thin client of `GET /v1/plugins`, as every read should be.

Nothing an install produces is live. Skills and bots land as DRAFTS behind their own publish
gates; a connector lands `pending_auth` until you authorize it; `stdio` servers are refused
outright - we do not execute imported binaries. An imported bundle can never add capability to
a running task by itself.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import typer

from prismoid.runner import client

plugins = typer.Typer(
    help="Agent Plugins bundles: install a directory, export a bot, read the receipts.",
    no_args_is_help=True,
)


@plugins.command("install")
def plugins_install(
    path: str = typer.Argument(..., help="A plugin directory (holds plugin.json)"),
    project: str | None = typer.Option(None, "--project", help="Install the bots into this Project"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Install a bundle: skills and bots become YOUR drafts, HTTP MCP servers become connectors.

    Per-component failure isolation, per the spec: a component that fails its door costs a line
    on the receipt, never the install. Publish each draft yourself once you have read it.
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        client.fail(f"{root} is not a directory", as_json=json_out)
    report = asyncio.run(_install(root, project, json_out))
    if json_out:
        client.emit(report, json_out)
        return
    typer.echo(f"installed {report['plugin']}" + (f" v{report['version']}" if report["version"] else ""))
    for item in report["items"]:
        mark = "✓" if item["ok"] else "-"
        typer.echo(f"  {mark} {item['kind']:<9} {item['name']:<24} {item['outcome']}")
    for note in report["notes"]:
        typer.echo(f"  · {note}")
    if any(i["kind"] in ("skill", "bot") and i["ok"] for i in report["items"]):
        typer.echo("\ndrafts land unpublished - test and publish each one you mean to use")


@plugins.command("export")
def plugins_export(
    name: str = typer.Argument(..., help="The bot to bundle"),
    out: str = typer.Option("plugins-out", "--out", help="Directory to write the bundle under"),
    project: str | None = typer.Option(None, "--project", help="The bot's Project (omit ⇒ personal)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Export one bot as a conformant plugin: the bot in our namespace, its skills portable,
    its connectors as mcp.json - URLs only, never a credential."""
    result = asyncio.run(_export(name, Path(out).expanduser(), project))
    if json_out:
        client.emit(result, json_out)
        return
    typer.echo(f"exported {name} → {result['path']}")
    for note in result["notes"]:
        typer.echo(f"  · {note}")


@plugins.command("list")
def plugins_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Your install receipts, newest first - what each bundle became."""
    page = client.request("GET", "/v1/plugins", as_json=json_out)
    rows = page["items"] if isinstance(page, dict) else page
    lines = []
    for r in rows:
        ok = sum(1 for i in r["items"] if i["ok"])
        lines.append(
            f"  {r['name']:<24} {('v' + r['version']) if r['version'] else '-':<10} "
            f"{ok}/{len(r['items'])} installed  {r['created_at'][:19]}"
        )
    client.emit(rows, json_out, human="\n".join(lines) or "no plugins installed yet")


async def _install(root: Path, project: str | None, json_out: bool) -> dict[str, Any]:
    from prismoid.ai.plugins.format import PluginError
    from prismoid.db.session import session_scope
    from prismoid.operations import plugins as ops
    from prismoid.runner.bots_cli import cli_actor  # the one credential→actor resolver

    async with session_scope() as session:
        actor = await cli_actor(session)
        try:
            report = await ops.install(
                session, actor, root, project_id=uuid.UUID(project) if project else None
            )
        except PluginError as exc:
            client.fail(str(exc), as_json=json_out)
        return report.model_dump(mode="json")


async def _export(name: str, out: Path, project: str | None) -> dict[str, Any]:
    from prismoid.db.session import session_scope
    from prismoid.operations import plugins as ops
    from prismoid.runner.bots_cli import cli_actor

    async with session_scope() as session:
        actor = await cli_actor(session)
        path, notes = await ops.export_bot(
            session, actor, name, out, project_id=uuid.UUID(project) if project else None
        )
        return {"path": str(path), "notes": notes}
