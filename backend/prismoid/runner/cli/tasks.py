# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid do` and the `prismoid tasks` group: submit a task, then list, show, steer, pause, continue,
fork and follow it. `prismoid tasks create` is `prismoid do`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from prismoid.runner import client, stream
from prismoid.runner.cli.choices import Clarify, Deliverable, DocFormat, Mode, Size, UsageBy
from prismoid.runner.cli.machine import MACHINE_WORDS, tasks_machine, tasks_screen
from prismoid.runner.place import place_on_device, submit
from prismoid.schemas.device import DOCKER_NOTE
from prismoid.schemas.device_away import device_label
from prismoid.schemas.inputs import MAX_INPUT_BYTES


def do(
    goal: str = typer.Argument(..., help="What you want done. Write acceptance criteria, not vibes."),
    *,
    project: str = typer.Option("", "--project", help="Run it inside a project."),
    mode: Mode = typer.Option(Mode.STANDARD, "--mode", help="A standard plan, or every model at once."),
    clarify: Clarify = typer.Option(
        Clarify.AUTO, "--clarify", help="Whether it may ask its one round of questions first."
    ),
    input: list[str] = typer.Option(
        [], "--input", "-i", help="Attach a source file. Repeatable. The verifier checks against these."
    ),
    skill: list[str] = typer.Option(
        [], "--skill", help="Pin a skill: `report`, `report@3`, or `report@draft` (yours). Repeatable."
    ),
    connector: list[str] = typer.Option(
        [], "--connector", help="Attach a connector by slug or id, e.g. `notion`. Repeatable."
    ),
    bot: list[str] = typer.Option(
        [],
        "--bot",
        help="Put a bot on this task's roster: `reviewer`, `reviewer@3`, or `reviewer@draft` "
        "(yours). Repeatable. Naming any NARROWS the roster to these - leave it empty and the "
        "project's published bots plus your own come along automatically.",
    ),
    deliverable: Deliverable | None = typer.Option(
        None,
        "--deliverable",
        help="Force the artifact kind (board is a collection). Unset, the planner reads the goal.",
    ),
    doc_format: DocFormat | None = typer.Option(
        None,
        "--format",
        help="Document file format (implies --deliverable document). Unset, your words decide, "
        "markdown by default.",
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Stream it, or fire and forget."),
    budget_usd: float = typer.Option(0.0, "--budget-usd", help="Cap the spend."),
    size: Size | None = typer.Option(
        None,
        "--size",
        help="Unset or auto: a literal number in the goal picks the size; otherwise standard, "
        "never pushed bigger.",
    ),
    units: int = typer.Option(
        0, "--units", "--pages", help="Size target in the kind's own unit: pages, slides, sheets, rows."
    ),
    folder: Path | None = typer.Option(
        None,
        "--folder",
        help="The folder a task on your computer works in, granted read-write. Default: the "
        "current folder, when the task runs on your computer (never sent to a cloud Prismoid).",
    ),
    docker: bool = typer.Option(
        False,
        "--docker",
        help=f"Let the task use Docker (on your computer). {DOCKER_NOTE}",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Submit a task. `--wait` streams it live; `--no-wait` returns the id and gets out of the way.

    `--input ./brief.md` puts a file in the sandbox at `/task/inputs/brief.md`, read-only, and makes
    it the **grounding set**: every number and quotation in the result is traced back to it, and one
    that cannot be traced fails the artifact. Sources are what turn "verified" into a claim with a
    definition.
    """
    body: dict[str, Any] = {"goal": goal, "mode": mode.value, "clarify": clarify.value}
    if project:
        body["project_id"] = project
    if budget_usd:
        body["budget"] = {"usd": budget_usd}
    if size:
        body["scale"] = size.value
    if units:
        body["target_pages"] = units  # the wire name predates the universal size dial
    if input:
        body["inputs"] = [_attach(p, json_out) for p in input]
    if skill:
        body["skills"] = skill
    if connector:
        body["connectors"] = connector
    if bot:
        body["bots"] = bot
    if deliverable:
        body["deliverable"] = deliverable.wire
    if doc_format:
        body["deliverable_format"] = doc_format.value
    defaulted = place_on_device(body, folder, docker, json_out)
    accepted = submit(body, defaulted, json_out)
    task_id = accepted["task_id"]

    if not wait:
        client.emit(
            accepted,
            json_out,
            human=f"task {task_id}\n  follow: prismoid tasks events {task_id} --follow",
        )
        return

    client.emit(accepted, json_out, human=f"task {task_id}")
    tasks_events(task_id, follow=True, since=0, json_out=json_out, answer=True)  # the demo surface


def _attach(path: str, json_out: bool) -> dict[str, str]:
    """A local file → a `TaskInputIn`. Read as **bytes** - a PDF is not a str.

    The filename sent is the *basename*: `/home/me/notes/q3.md` becomes `q3.md`, because the frame
    path is `/task/inputs/<filename>` and the user's directory layout is not the sandbox's business.
    The server validates it again anyway - a client-side check is a courtesy, never a control.
    """
    import base64
    import mimetypes

    file = Path(path)
    if not file.is_file():
        client.fail(f"no such file: {path}", as_json=json_out)

    data = file.read_bytes()
    if len(data) > MAX_INPUT_BYTES:
        client.fail(
            f"{file.name}: {len(data):,} bytes exceeds the {MAX_INPUT_BYTES:,} limit", as_json=json_out
        )

    guessed, _ = mimetypes.guess_type(file.name)
    return {
        "filename": file.name,
        "content": base64.b64encode(data).decode(),
        "media_type": guessed or "text/plain",
    }


tasks = typer.Typer(help="Tasks: list, show, follow.", no_args_is_help=True)
tasks.command("create")(do)  # `prismoid tasks create` is `prismoid do`


@tasks.command("list")
def tasks_list(
    status: str = typer.Option("", "--status"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List your tasks (and every task in a project you belong to)."""
    params = {"status": status} if status else None
    page = client.request("GET", "/v1/tasks", params=params, as_json=json_out)
    rows = page["items"]
    lines = [f"{r['id']}  {r['status']:<10}  {r['goal'][:60]}" for r in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no tasks")


@tasks.command("show")
def tasks_show(
    task_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """A task's plan, states, and cost so far."""
    # An empty id would request `/v1/tasks/`, which redirects to the *list* - and the caller then
    # gets a page where it expected a task, and a KeyError three lines later. Cheap guard, because
    # `prismoid tasks show $ID` with an unset $ID is a thing people do at 2am.
    if not task_id.strip():
        client.fail("no task id given", as_json=json_out)
    task = client.request("GET", f"/v1/tasks/{task_id}", as_json=json_out)
    client.emit(task, json_out, human=_show(task))


# A step list with a count and no steps is not a plan, it is a rumour about one. `tasks show` is
# where a human reads what the machine decided to do - the agents it picked, what waits on what, and
# the standard the work will be held to. All three were being dropped on the floor (see
# `operations/planning_steps.plan_view`).
_MARK = {
    "done": "✓",
    "running": "▸",
    "verifying": "◇",
    "retrying": "↻",
    "escalated": "!",
    "failed": "✗",
    "skipped": "–",
}


def _show(task: dict[str, Any]) -> str:
    plan = task["plan"]
    steps = plan["steps"]
    order = {s["id"]: n for n, s in enumerate(steps, 1)}
    waits: dict[str, list[int]] = {}
    for e in plan.get("edges", []):
        waits.setdefault(e["to_step"], []).append(order.get(e["from_step"], 0))

    lines = [
        task["id"],
        f"  status:  {task['status']}",
        f"  goal:    {task['goal']}",
        f"  cost:    ${task['cost']['total_usd']}  ({task['cost']['calls']} call(s))",
    ]
    # The resolved size: even an `auto` task shows which of the five it became.
    # `size_marker` is the server's own composition ("extended · ~15 pages"), unit included -
    # this surface can never drift from the table again.
    if task.get("size_marker"):
        lines.insert(2, f"  size:    {task['size_marker']}")

    if steps:
        lines.append(f"\n  plan - {len(steps)} step(s)")
        for n, s in enumerate(steps, 1):
            after = waits.get(s["id"])
            needs = f"  (after {', '.join(f'#{d}' for d in sorted(after))})" if after else ""
            attempt = f"  attempt {s['attempt']}" if s.get("attempt", 0) > 1 else ""
            lines.append(
                f"    {_MARK.get(s['state'], '·')} #{n} [{s['agent'] or '-'}] {s['title']}{needs}{attempt}"
            )

    if must := plan.get("criteria", {}).get("must"):
        lines.append("\n  held to")
        lines.extend(f"    · {m}" for m in must)

    if m := task.get("machine"):
        lines.append(f"  machine: {MACHINE_WORDS.get(str(m['state']), m['state'])} · {m['profile']}")
        # A device Machine has no screen: its browser is a window on the user's own computer.
        if m.get("driver") == "device" and m.get("profile") == "browser":
            lines.append(f"  Browser on {device_label(m.get('device_name'))}")
    if task.get("waiting_for"):  # a step waiting for its device: "Waiting for MacBook Pro"
        lines.append(f"  {task['waiting_for']}")

    return "\n".join(lines)


@tasks.command("steer")
def tasks_steer(
    task_id: str = typer.Argument(...),
    directive: str = typer.Argument("", help='What to change, e.g. "use euros".'),
    epoch: int | None = typer.Option(None, "--epoch", help="the plan epoch you are steering from"),
    retract: str | None = typer.Option(
        None, "--retract", help="withdraw your own unapplied steer by its command id"
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Steer a live task. The running step takes it at its next safe point.

    With `--epoch`, a steer issued against an older plan epoch is refused with what you missed
    (exit 4): someone steered, or started the next turn, while you were typing."""
    if retract:
        r = client.request("POST", f"/v1/tasks/{task_id}/steers/{retract}/retract", as_json=json_out)
        client.emit(r, json_out, human=f"retracted {retract}")
        return
    if not directive.strip():
        client.fail("a steer needs words", client.EXIT_ERROR, json_out)
    body: dict[str, Any] = {"directive": directive}
    if epoch is not None:
        body["issued_epoch"] = epoch
    try:
        r = client.request(
            "POST", f"/v1/tasks/{task_id}/steer", body=body, as_json=json_out, raise_conflict=True
        )
    except client.Conflicted as stale:
        if json_out:
            typer.echo(json.dumps({"error": stale.error}), err=True)
        else:  # a teammate's name and words reach YOUR terminal: printable or not at all
            typer.secho(f"error: {stream.printable(str(stale.error.get('message')))}", fg="red", err=True)
            for m in stale.error.get("missed") or []:
                line = f"{m.get('author')} {m.get('kind')} {m.get('text', '')}".rstrip()
                typer.echo(f"  missed: {stream.printable(line)}", err=True)
        raise typer.Exit(client.EXIT_CONFLICT) from None
    client.emit(
        r, json_out, human=f"steered (epoch {r['epoch']}, {r['state']}): {directive}  [{r['command_id']}]"
    )


@tasks.command("move")
def tasks_move(
    task_id: str = typer.Argument(...),
    project: str = typer.Option("", "--project", help="Move the task into this project."),
    personal: bool = typer.Option(False, "--personal", help="Move it out of any project, back to private."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Move a task in, out, or between projects. Past spend stays where it was billed; new spend follows."""
    if not project and not personal:
        client.fail("give --project <id> or --personal", as_json=json_out)
    r = client.request(
        "POST",
        f"/v1/tasks/{task_id}/move",
        body={"project_id": None if personal else project},
        as_json=json_out,
    )
    client.emit(r, json_out, human="moved to personal" if personal else f"moved to project {project}")


@tasks.command("budget")
def tasks_budget(
    task_id: str = typer.Argument(...),
    usd: str = typer.Option(..., "--usd", help="The task's new USD ceiling, e.g. 5 or 2.50."),
    resume: bool = typer.Option(False, "--resume", help="Then accept the task's open budget gate."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Set a task's budget - the answer to "Budget reached" (B3). An editor of the task's Project."""
    r = client.request("PUT", f"/v1/tasks/{task_id}/budget", body={"usd": usd}, as_json=json_out)
    human = f"budget ${r['usd']} (was {'$' + str(r['previous_usd']) if r.get('previous_usd') else 'none'})"
    if resume:
        # The raise has LANDED: a resume that cannot follow it still reports it - in --json too -
        # then exits non-zero with the reason.
        refused: tuple[str, int] | None = None
        if not r.get("gate_id"):
            refused = ("the budget is set, but there is no open budget gate to resume", client.EXIT_ERROR)
        else:
            try:
                r["resolved"] = client.request(
                    "POST",
                    f"/v1/gates/{r['gate_id']}/resolve",
                    body={"action": "accept"},
                    as_json=json_out,
                    raise_refused=True,
                )
            except client.Refused as no:
                refused = (f"the budget is set, but the resume was refused - {no}", no.code)
        if refused is not None:
            r["resumed"], r["error"] = False, refused[0]
            client.emit(r, json_out, human=human)
            client.fail(refused[0], refused[1], as_json=json_out)
        r["resumed"] = True
    client.emit(r, json_out, human=human + ("; resumed" if resume else ""))


@tasks.command("replan")
def tasks_replan(
    task_id: str = typer.Argument(...),
    reason: str = typer.Argument(..., help="Why the plan needs rewriting."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Force the planner to rewrite the path - recorded in plan_edits, done steps kept."""
    r = client.request("POST", f"/v1/tasks/{task_id}/replan", body={"directive": reason}, as_json=json_out)
    client.emit(r, json_out, human=f"replanned: {r.get('steps', '?')} new step(s)")


@tasks.command("pause")
def tasks_pause(task_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """Pause the task: the running step parks at its next tool call (if it can), or the task holds
    at the next boundary. `resume` picks up exactly there."""
    r = client.request("POST", f"/v1/tasks/{task_id}/pause", as_json=json_out)
    client.emit(r, json_out, human="pausing at the next safe point")


tasks.command("machine")(tasks_machine)
tasks.command("screen")(tasks_screen)


@tasks.command("resume")
def tasks_resume(task_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """Un-pause a paused task."""
    r = client.request("POST", f"/v1/tasks/{task_id}/resume", as_json=json_out)
    client.emit(r, json_out, human="resumed")


@tasks.command("cancel")
def tasks_cancel(task_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")) -> None:
    """Stop now - the only mid-step interrupt."""
    r = client.request("POST", f"/v1/tasks/{task_id}/cancel", as_json=json_out)
    client.emit(r, json_out, human="cancelled")


@tasks.command("follow-up")
def tasks_follow_up(
    task_id: str = typer.Argument(..., help="The settled task to continue."),
    goal: str = typer.Argument(..., help="What to do next - the thread's next turn."),
    *,
    wait: bool = typer.Option(False, "--wait/--no-wait"),
    size: Size | None = typer.Option(
        None, "--size", help="Change the thread's size for this turn; unset keeps it."
    ),
    units: int = typer.Option(0, "--units", "--pages", help="Size target in the kind's own unit."),
    deliverable: Deliverable | None = typer.Option(
        None,
        "--deliverable",
        help="Force this turn's artifact kind (board is a collection). Unset, the turn decides again: "
        "fresh words about the kind change it, silence keeps the thread's.",
    ),
    doc_format: DocFormat | None = typer.Option(
        None, "--format", help="This turn's document format (implies document)."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Continue the SAME task with a new message - the thread is the task.

    `now translate that into French` runs as the next turn, with the whole conversation and
    every earlier deliverable in hand. A live task refuses (steer it instead); fork branches.
    """
    body: dict[str, Any] = {"goal": goal}
    if size:
        body["scale"] = size.value
    if units:
        body["target_pages"] = units
    if deliverable:
        body["deliverable"] = deliverable.wire
    if doc_format:
        body["deliverable_format"] = doc_format.value
    accepted = client.request("POST", f"/v1/tasks/{task_id}/follow_up", body=body, as_json=json_out)
    same = accepted["task_id"]
    turn = accepted.get("turn", "?")
    # Replay from THIS gesture's frame: `since` is exclusive, and the task.continued frame was
    # the record's head at accept - so the follow starts there and earlier turns' terminal
    # frames can never end it early (runner/stream exits on the first terminal it sees).
    since = max(int(accepted.get("last_seq", 1)) - 1, 0)
    if wait:
        client.emit(accepted, json_out, human=f"continuing task {same} (turn {turn})")
        tasks_events(same, follow=True, since=since, json_out=json_out, answer=True)
    else:
        client.emit(
            accepted,
            json_out,
            human=(
                f"continuing task {same} (turn {turn})\n  follow: prismoid tasks events {same} -f --since {since}"
            ),
        )


@tasks.command("retry")
def tasks_retry(
    task_id: str = typer.Argument(..., help="The settled task whose latest ask to run again."),
    wait: bool = typer.Option(False, "--wait/--no-wait"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run the latest ask again as a new turn - the thread, including what went
    wrong, rides into the planner. A live task refuses; steer it instead."""
    accepted = client.request("POST", f"/v1/tasks/{task_id}/retry", as_json=json_out)
    same = accepted["task_id"]
    turn = accepted.get("turn", "?")
    since = max(int(accepted.get("last_seq", 1)) - 1, 0)
    if wait:
        client.emit(accepted, json_out, human=f"retrying task {same} (turn {turn})")
        tasks_events(same, follow=True, since=since, json_out=json_out, answer=True)
    else:
        client.emit(
            accepted,
            json_out,
            human=(
                f"retrying task {same} (turn {turn})\n  follow: prismoid tasks events {same} -f --since {since}"
            ),
        )


@tasks.command("fork")
def tasks_fork(
    task_id: str = typer.Argument(..., help="The task to branch from."),
    folder: Path | None = typer.Option(
        None, "--folder", help="The folder the fork works in, on your computer. Default: a fresh one."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Branch the thread: a NEW held task carrying the whole conversation and the
    parent's dials. It runs nothing until you steer it with a direction. A fork of a task on your
    computer never copies its folder: it works in `--folder`, or a fresh one."""
    body = {"folder": str(folder.expanduser().absolute())} if folder is not None else {}
    r = client.request("POST", f"/v1/tasks/{task_id}/fork", body=body, as_json=json_out)
    child = r["task_id"]
    client.emit(
        r,
        json_out,
        human=f'forked → task {child} (held)\n  launch: prismoid tasks steer {child} "<direction>"',
    )


@tasks.command("usage")
def tasks_usage(
    task_id: str = typer.Argument(...),
    by: UsageBy | None = typer.Option(None, "--by", help="Break the cost down by model family or by person."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """What a task cost. Every provider call, priced at the moment it was made."""
    usage = client.request("GET", f"/v1/tasks/{task_id}/usage", as_json=json_out)
    if by is UsageBy.FAMILY:
        lines = [f"  {f:<12} ${c}" for f, c in usage["by_family"].items()]
    elif by is UsageBy.PERSON:
        lines = [f"  {p['name']:<20} ${p['usd']}" for p in usage["by_person"]]
    else:
        lines = [f"  {u:<20} {n:>10,}" for u, n in usage["units"].items()]
    lines.append(f"  {'TOTAL':<20} ${usage['total_usd']}   ({usage['calls']} calls)")
    client.emit(usage, json_out, human="\n".join(lines))


@tasks.command("events")
def tasks_events(
    task_id: str = typer.Argument(...),
    follow: bool = typer.Option(
        False,
        "--follow/--no-follow",
        "-f",
        help="Stay attached (the flight recorder); the default replays and exits.",
    ),
    since: int = typer.Option(0, "--since", help="Replay from this seq."),
    json_out: bool = typer.Option(False, "--json"),
    answer: bool = False,  # not a flag: only `prismoid do --wait` asks. A recorder never prompts.
) -> None:
    """The terminal flight recorder. `--since 0` replays the whole history.

    In a shared Project each line names who did it, and --follow shows who is here and who is typing
    (`--json` prints those presence frames raw).
    """
    stream.follow_events(task_id, since=since, follow=follow, json_out=json_out, answer=answer)
