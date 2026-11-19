# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The numbered batch, in a terminal.

One batch, numbered `1)` `2)` `3)`, answered inline. The numbering is **the server's** - it arrives
on the `gate.opened` frame already ordered, and `q1` is question 1 on every surface there is. A CLI
that numbered them itself would be a second place for the numbering to disagree.

Three ways out, and all three are on screen, because a question you cannot escape is a hostage
situation: answer them, `skip` (proceed on the defaults - the work still happens), or `cancel`.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import typer

from prismoid.runner import client
from prismoid.runner.terminal import printable
from prismoid.schemas.device_away import device_label

MARK = {"single_select": "pick one", "multi_select": "pick any", "boolean": "yes / no"}


def interactive() -> bool:
    """A prompt written to a pipe is a prompt nobody will ever answer, and the task waits 24 hours
    to find that out. If nobody is at the keyboard, we do not ask - we let the gate stand, and the
    user resolves it later with `prismoid gates resolve`."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def render(questions: dict[str, Any]) -> None:
    """Every string here was written by a model or a server, so each reaches the terminal through
    `printable`: an escape sequence must not repaint or hide the question being answered."""
    typer.secho(f"\n  {printable(str(questions['title']))}", bold=True)
    if context := questions.get("context"):
        typer.secho(f"  {printable(str(context))}\n", dim=True)

    for n, q in enumerate(questions["questions"], start=1):
        required = "" if q.get("default") else typer.style("  (required)", fg=typer.colors.YELLOW)
        typer.echo(f"  {n}) {printable(str(q['prompt']))}{required}")

        if options := q.get("options"):
            for letter, option in zip("abcdefgh", options, strict=False):
                typer.echo(f"       {letter}) {printable(str(option['label']))}")
        elif hint := MARK.get(q["type"]):
            typer.secho(f"       {hint}", dim=True)

        if (default := q.get("default")) is not None:
            typer.secho(f"       default: {printable(str(default))}", dim=True)

    typer.secho(
        "\n  answer as  1: b, 2: yes  ·  or 'skip' to proceed on the defaults  ·  or 'cancel'", dim=True
    )


def parse(reply: str, questions: dict[str, Any]) -> dict[str, str]:
    """`1: b, 2: yes` -> `{"q1": "b", "q2": "yes"}`; a bare reply answers a single question.
    Only splits the line: validation is the server's (`operations/questions.py`)."""
    ids = [q["id"] for q in questions["questions"]]

    if ":" not in reply and len(ids) == 1:
        return {ids[0]: reply.strip()}

    answers: dict[str, str] = {}
    for chunk in reply.split(","):
        number, _, value = chunk.partition(":")
        number, value = number.strip(), value.strip()
        if number.isdigit() and 1 <= int(number) <= len(ids):
            answers[ids[int(number) - 1]] = value
    return answers


def ask(gate_id: str, questions: dict[str, Any], *, as_json: bool) -> None:
    """Render, read, resolve. Loops on a 422 - the server said which question was wrong, and the
    user is two keystrokes from fixing it. Re-asking the whole batch would be punishing them for a
    typo."""
    render(questions)

    while True:
        reply = typer.prompt("\n  >", prompt_suffix=" ").strip()

        if reply.casefold() in ("skip", "decline", ""):
            client.request(
                "POST", f"/v1/gates/{gate_id}/resolve", body={"action": "decline"}, as_json=as_json
            )
            typer.secho("  proceeding on the defaults", dim=True)
            return

        if reply.casefold() in ("cancel", "stop", "quit"):
            client.request("POST", f"/v1/gates/{gate_id}/resolve", body={"action": "cancel"}, as_json=as_json)
            typer.secho("  cancelled", fg=typer.colors.YELLOW)
            return

        try:
            client.request(
                "POST",
                f"/v1/gates/{gate_id}/resolve",
                body={"action": "accept", "content": parse(reply, questions)},
                as_json=as_json,
                raise_invalid=True,
            )
            return
        except client.Invalid as why:  # 422 - and it named the question
            typer.secho(f"  {why}", fg=typer.colors.RED)


_SCOPE_WORDS = {
    "once": "once",
    "o": "once",
    "task": "task",
    "t": "task",
    # No single letter for `always`: it writes a STANDING allowance for every future task, and
    # a one-keystroke typo ("a" for "allow") must never make that promise. The full word only.
    "always": "always",
}
_ALLOW_WORDS = {"allow", "yes", "y", "approve", "ok", "accept"}


def match_scope(reply: str, scopes: Sequence[str]) -> str | None:
    """A typed reply against the scopes THIS gate offers - the scope's own name
    or its first letter picks it directly (`always` only by its full name); a bare "allow" (or a synonym) picks "task" when
    offered, else the gate's own first scope. `None` when the gate offers nothing to pick, or
    the reply matches none of it (both fall through to decline - see `ask_decision`). Pure,
    so the terminal's word matching is tested without a `typer.prompt`."""
    if not scopes:
        return None
    reply = reply.strip().casefold()
    chosen = _SCOPE_WORDS.get(reply)
    if chosen is None and reply in _ALLOW_WORDS:
        chosen = "task" if "task" in scopes else scopes[0]
    return chosen if chosen in scopes else None


def ask_decision(
    gate_id: str,
    *,
    title: str,
    reason: str,
    as_json: bool,
    scopes: Sequence[str] = (),
    host: str = "",
) -> None:
    """A questionless gate (approve / connect): no numbered batch, just a decision -
    or, for a browser allowlist ask, a decision AND a scope (once/task/always).

    Renders the title and its evidence/why, then asks the one binary - allow it, or skip
    (proceed without). `cancel` stops the task, as everywhere. There is nothing to parse and no
    422 to loop on for a plain gate (`scopes` empty): accept carries no content. For a gate that
    offers scopes, a bare "allow" picks "task" (or the gate's first offered scope); typing the
    scope's name (or its first letter; `always` only in full) picks it directly."""
    # The title and the evidence are a model's or a server's words: printable, so an escape
    # sequence cannot repaint the approval the person is about to give.
    typer.secho(f"\n  {printable(title)}", bold=True)
    if reason:
        for line in reason.splitlines():
            typer.secho(f"  {printable(line)}", dim=True)
    if scopes:
        labels = {
            "once": "once (o)",
            "task": "for this task (t)",
            "always": f"always allow {printable(host)} (type 'always')",
        }
        offer = "  ·  ".join(labels[s] for s in scopes if s in labels)
        typer.secho(f"\n  allow: {offer}  ·  or 'skip' to proceed without it  ·  or 'cancel'", dim=True)
    else:
        typer.secho("\n  allow  ·  or 'skip' to proceed without it  ·  or 'cancel'", dim=True)

    reply = typer.prompt("\n  >", prompt_suffix=" ").strip().casefold()
    if reply in ("cancel", "stop", "quit"):
        client.request("POST", f"/v1/gates/{gate_id}/resolve", body={"action": "cancel"}, as_json=as_json)
        typer.secho("  cancelled", fg=typer.colors.YELLOW)
        return

    if scopes:
        chosen = match_scope(reply, scopes)
        if chosen is not None:
            client.request(
                "POST",
                f"/v1/gates/{gate_id}/resolve",
                body={"action": "accept", "content": {"scope": chosen}},
                as_json=as_json,
            )
            typer.secho(f"  allowed ({chosen})", fg=typer.colors.GREEN)
            return
    elif reply in _ALLOW_WORDS:
        client.request("POST", f"/v1/gates/{gate_id}/resolve", body={"action": "accept"}, as_json=as_json)
        typer.secho("  allowed", fg=typer.colors.GREEN)
        return
    # Anything else - "skip", "no", empty, or (with scopes offered) an unmatched reply -
    # proceeds without (the safe default for a side effect).
    client.request("POST", f"/v1/gates/{gate_id}/resolve", body={"action": "decline"}, as_json=as_json)
    typer.secho("  proceeding without it", dim=True)


_RESUME_WORDS = {"resume", "r", "continue", *_ALLOW_WORDS}
_CANCEL_WORDS = ("cancel", "stop", "quit")


def ask_budget(gate_id: str, *, title: str, reason: str, scope: str, task_id: str, as_json: bool) -> None:
    """A budget gate - "Budget reached" (B3) or the long-form cost confirmation: resume, or cancel.

    Never a decline: declining a budget gate CANCELS the task (`run_task._hold_for_budget`), so
    `ask_decision`'s "skip to proceed without it" sent decline and printed "proceeding" over a
    task it had just stopped. Here Enter (or "skip") leaves the gate standing - the
    task waits, nothing is spent, and it can be raised and resumed from another terminal while
    this one keeps watching. A refused resume (a Project still over its owner's ceiling) says why
    and asks again."""
    typer.secho(f"\n  {title}", bold=True)
    for line in (reason or "").splitlines():
        typer.secho(f"  {line}", dim=True)
    if scope == "project":
        typer.secho(
            "  only the Project's owner can raise its budget (Project settings); resume once they have",
            dim=True,
        )
    elif title == "Budget reached":
        typer.secho(
            f"  to raise it and resume: prismoid tasks budget {task_id} --usd <amount> --resume", dim=True
        )
    typer.secho(
        "\n  'resume' to run on as it is  ·  'cancel' stops the task  ·  Enter leaves it waiting", dim=True
    )
    while True:
        reply = typer.prompt("\n  >", prompt_suffix=" ", default="", show_default=False).strip().casefold()
        if reply in _CANCEL_WORDS:
            client.request("POST", f"/v1/gates/{gate_id}/resolve", body={"action": "cancel"}, as_json=as_json)
            typer.secho("  cancelled", fg=typer.colors.YELLOW)
            return
        if reply in _RESUME_WORDS:
            try:
                client.request(
                    "POST",
                    f"/v1/gates/{gate_id}/resolve",
                    body={"action": "accept"},
                    as_json=as_json,
                    raise_refused=True,
                )
            except client.Refused as no:
                typer.secho(f"  {no}", fg=typer.colors.RED)
                continue
            typer.secho("  resumed", fg=typer.colors.GREEN)
            return
        typer.secho(
            f"  left waiting - resolve it later with `prismoid gates resolve {gate_id}`, or keep watching here",
            dim=True,
        )
        return


_DONE_WORDS = {"done", "d", "finished", *_ALLOW_WORDS}
_SKIP_WORDS = {"skip", "s", "no"}


def ask_done(gate_id: str, *, title: str, device: str | None, as_json: bool) -> None:
    """A device task's hand-off: the agent asked for someone at the
    browser, and its browser is a window in the Prismoid Chrome on the user's own computer - no screen,
    no takeover. The person does what it asks there, then says `done` (the creator's accept: the
    window is handed back). `skip` lets the agent go on without it (a decline), `cancel` stops the
    task, and Enter leaves the gate standing, to be answered later or from the web."""
    typer.secho(f"\n  {title}", bold=True)
    typer.secho(f"  Browser on {device_label(device)}: do it in its Prismoid Chrome window.", dim=True)
    typer.secho(
        "\n  'done' once you have  ·  'skip' to go on without it  ·  'cancel' stops the task  ·  "
        "Enter leaves it waiting",
        dim=True,
    )
    said = {"cancel": "  cancelled", "accept": "  handed back", "decline": "  going on without it"}
    colour = {"cancel": typer.colors.YELLOW, "accept": typer.colors.GREEN}
    while True:
        reply = typer.prompt("\n  >", prompt_suffix=" ", default="", show_default=False).strip().casefold()
        action = (
            "cancel"
            if reply in _CANCEL_WORDS
            else "accept"
            if reply in _DONE_WORDS
            else "decline"
            if reply in _SKIP_WORDS
            else None
        )
        if action is None:
            typer.secho(
                f"  left waiting - say done later with `prismoid gates resolve {gate_id} --done`, or in the web",
                dim=True,
            )
            return
        try:
            client.request(
                "POST",
                f"/v1/gates/{gate_id}/resolve",
                body={"action": action},
                as_json=as_json,
                raise_refused=True,
            )
        except client.Refused as no:  # a refused or unreachable answer says why and asks again
            typer.secho(f"  {no}", fg=typer.colors.RED)
            continue
        typer.secho(said[action], fg=colour.get(action), dim=action == "decline")
        return
