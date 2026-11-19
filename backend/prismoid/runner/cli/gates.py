# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid gates list|resolve` - the questions waiting on you, answered from the terminal."""

from __future__ import annotations

from typing import Any

import typer

from prismoid.runner import client
from prismoid.runner import questions as runner_questions
from prismoid.runner.cli.choices import ApproveScope

gates = typer.Typer(help="Questions waiting on you: list and resolve.", no_args_is_help=True)


@gates.command("list")
def gates_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Every question waiting on you - the queue, not the notification."""
    rows = client.request("GET", "/v1/gates", as_json=json_out)
    lines = []
    for g in rows:
        n = len((g.get("questions") or {}).get("questions", []))
        lines.append(f"{g['id']}  {g['title']}  ({n} question{'s' if n != 1 else ''})")
    client.emit(rows, json_out, human="\n".join(lines) or "nothing waiting on you")


_RESOLVED = {"accept": "accepted", "decline": "declined", "cancel": "cancelled"}


@gates.command("resolve")
def gates_resolve(
    gate_id: str = typer.Argument(..., help="The gate id (from `prismoid gates list`)."),
    *,
    answers: str = typer.Option(
        "", "-a", "--answers", help='Answers, numbered: "1: b, 2: yes". Same shape as the prompt.'
    ),
    accept: bool = typer.Option(
        False, "--accept", help="Allow a questionless gate (an approve/connect gate has no answers)."
    ),
    done: bool = typer.Option(
        False,
        "--done",
        help="Hand the browser back on your own computer: you did what the agent asked in its Prismoid "
        "Chrome window (a device task's hand-off).",
    ),
    scope: ApproveScope | None = typer.Option(
        None,
        "--scope",
        help="How far a browser approval reaches, for a gate that offers scopes. Unset, the gate's "
        "default ('task', or its first offered scope); ignored for a gate that offers none.",
    ),
    decline: bool = typer.Option(False, "--decline", help="Proceed on the defaults, without answering."),
    cancel: bool = typer.Option(False, "--cancel", help="Stop the task."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Resolve a gate - the three actions, from the terminal: one you walked away from, or a
    teammate's in a Project you edit (the gate id is on its `gate.opened` line in `tasks events`).

    `--decline` and `--cancel` are the two no-answer actions; `--accept` allows a questionless
    gate (approve / connect); otherwise `-a` carries the answers, validated
    server-side exactly as the inline prompt's are (a 422 names the question).
    """
    if cancel:
        action, content = "cancel", None
    elif done:  # a device hand-off's Done is its creator's accept
        action, content = "accept", None
    elif accept:
        # A browser approve gate offers scopes; with no `--scope` the server takes
        # the gate's default, so a Project editor - whose `GET /v1/gates` never lists the
        # creator's gate - accepts exactly as the creator does.
        action, content = "accept", ({"scope": scope.value} if scope else None)
    elif decline or not answers:
        action, content = "decline", None
    else:
        this = client.request("GET", f"/v1/gates/{gate_id}", as_json=json_out)
        if not this.get("questions"):
            client.fail(
                f"gate {gate_id} takes no answers - use --accept to allow it, or --decline to skip",
                as_json=json_out,
            )
        action, content = "accept", runner_questions.parse(answers, this["questions"])

    body: dict[str, Any] = {"action": action}
    if content is not None:
        body["content"] = content
    result = client.request("POST", f"/v1/gates/{gate_id}/resolve", body=body, as_json=json_out)
    client.emit(result, json_out, human=f"gate {_RESOLVED[action]}")
