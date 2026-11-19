# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid inbox` - what the system told you while you were not watching.

prismoid inbox [--unread] · read ID · read --task ID · read --all
"""

from __future__ import annotations

import typer

from prismoid.runner import client

inbox = typer.Typer(help="What the system told you while you were not watching.", no_args_is_help=False)


@inbox.callback(invoke_without_command=True)
def inbox_list(
    ctx: typer.Context,
    unread: bool = typer.Option(False, "--unread", help="Only what you have not seen."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Your inbox. **Every item points at the thing it is about** - an inbox item you cannot act on
    from is just noise with a timestamp, so each line ends with the command that follows it."""
    if ctx.invoked_subcommand is not None:
        return

    result = client.request("GET", f"/v1/inbox?unread={str(unread).lower()}", as_json=json_out)
    items = result["items"]
    if not items:
        client.emit(result, json_out, human="nothing unread" if unread else "inbox empty")
        return

    mark = {"escalation": "✗", "task_completed": "✓", "gate_opened": "?", "budget": "$"}
    lines: list[str] = []
    for i in items:
        dot = " " if i["read"] else "•"
        lines.append(f"{dot} {mark.get(i['kind'], '·')} {i['title']}")
        first = (i["body"] or "").strip().splitlines()
        if first:
            lines.append(f"      {first[0][:100]}")
        if i["task_id"]:
            lines.append(f"      → prismoid tasks show {i['task_id']}")
        lines.append("")
    lines.append(f"{result['unread']} unread")

    client.emit(result, json_out, human="\n".join(lines))


@inbox.command("read")
def inbox_read(
    item_id: str = typer.Argument("", help="The item to mark read. Omit with --all or --task."),
    all_: bool = typer.Option(False, "--all", help="Mark everything read."),
    task_id: str = typer.Option("", "--task", help="Mark your items about this task read."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mark an item - everything, or everything about one task - as read."""
    if all_:
        result = client.request("POST", "/v1/inbox/read-all", as_json=json_out)
        client.emit(result, json_out, human="all read")
        return
    if task_id:
        result = client.request("POST", f"/v1/inbox/tasks/{task_id}/read", as_json=json_out)
        client.emit(result, json_out, human=f"read {result['marked']} · {result['unread']} unread")
        return
    if not item_id:
        client.fail("give an item id, --task, or --all", as_json=json_out)

    result = client.request("POST", f"/v1/inbox/{item_id}/read", as_json=json_out)
    client.emit(result, json_out, human=f"read: {result['title']}")
