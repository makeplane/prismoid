# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid capture` - soft intake: jot an idea now, make it a task later.

prismoid capture add TEXT [--url U] [--project P] · list · promote ID [--goal G]
"""

from __future__ import annotations

from typing import Any

import typer

from prismoid.runner import client

capture = typer.Typer(help="Capture: jot an idea now, make it a task later.", no_args_is_help=True)


@capture.command("add")
def capture_add(
    text: str = typer.Argument(..., help="The idea. Runs nothing until you promote it."),
    url: str = typer.Option("", "--url", help="A link to remember with it."),
    project: str = typer.Option("", "--project", help="File it in a project (editor+)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Save a thought. Capture runs nothing - that is the point."""
    body: dict[str, Any] = {"text": text}
    if url:
        body["url"] = url
    if project:
        body["project_id"] = project
    c = client.request("POST", "/v1/capture", body=body, as_json=json_out)
    client.emit(c, json_out, human=f"{c['id']}  captured")


@capture.command("list")
def capture_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Your open captures - the ideas not yet started."""
    rows = client.request("GET", "/v1/capture", as_json=json_out)
    lines = [f"{c['id']}  {c['text'][:60]}" for c in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "nothing captured")


@capture.command("promote")
def capture_promote(
    capture_id: str = typer.Argument(...),
    goal: str = typer.Option("", "--goal", help="Sharpen the goal (default: the capture's text)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Turn a capture into a task - the first moment a machine runs."""
    t = client.request("POST", f"/v1/capture/{capture_id}/promote", body={"goal": goal}, as_json=json_out)
    client.emit(t, json_out, human=f"promoted → task {t['task_id']}")
