# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid bots` - user-authored agents, as a command.

    prismoid bots init contract-reviewer     # scaffold a plugin directory to edit
    prismoid bots push contract-reviewer     # new draft + auto-run the selection eval
    prismoid bots publish contract-reviewer  # 409 unless the evals are green
    prismoid bots list                       # the roster One can route across

The loop the skills lane proved, with the four things it was missing. `init` scaffolds, because
every author's first move today is copying someone else's directory. `diff` shows what a version
actually changed. `test` is separated from `push` for when you only want the score. And `pull`
round-trips, so an edit never has to be retyped.

`test` is the exception to "the CLI is a thin API client", and for the reason the skills
lane recorded: a selection case is a **model call**, and the API server does no model work.
The eval runs here, in-process, and writes its verdict to the column `publish` reads.
(`POST /v1/bots/{name}/test` exists too, but it only ENQUEUES the same eval onto
the worker for web authors - the API process still never runs a model.)
"""

from __future__ import annotations

import asyncio
import difflib
import uuid
from pathlib import Path
from typing import Any

import typer

from prismoid.errors import PrismoidError
from prismoid.runner import client

bots = typer.Typer(
    help="Bots: your own agents. Data, not deploys - draft → test → publish.",
    no_args_is_help=True,
)

WORK_DIR = Path("bots-work")  # where `init` and `pull` put a plugin directory for editing

_PROJECT = typer.Option(None, "--project", help="The Project this bot belongs to (omit ⇒ personal)")


def _params(project: str | None) -> dict[str, Any]:
    return {"project_id": project} if project else {}


# ── authoring ─────────────────────────────────────────────────────────────────────────────────


@bots.command("init")
def bots_init(
    name: str = typer.Argument(..., help="The bot's name, e.g. contract-reviewer"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Scaffold an Agent Plugins directory for a new bot, with a commented starter BOT.md.

    The layout is the standard's (agent-plugins.org), so the bundle is portable: `skills/` and
    `mcp.json` are components any conformant client understands, and the bot itself sits in the
    reverse-domain namespace the spec reserves for client-specific parts.
    """
    from prismoid.ai.bots import format as bot_format

    root = WORK_DIR / name
    bot_dir = root / bot_format.NAMESPACE / bot_format.BOTS_DIR / name
    if bot_dir.exists():
        client.fail(f"{bot_dir} already exists - edit it, or pick another name", as_json=json_out)
    bot_dir.mkdir(parents=True)
    (root / "skills").mkdir(exist_ok=True)
    manifest_path = _write_plugin_manifest(root, name)
    (bot_dir / bot_format.BOT_FILE).write_text(_STARTER_BOT.format(name=name))
    (bot_dir / bot_format.CASES_FILE).write_text(_STARTER_CASES.format(name=name))

    created = [
        str(manifest_path),
        str(bot_dir / bot_format.BOT_FILE),
        str(bot_dir / bot_format.CASES_FILE),
    ]
    client.emit(
        {"name": name, "path": str(root), "created": created},
        json_out,
        human=(
            f"scaffolded {root}\n  edit  {bot_dir / bot_format.BOT_FILE}\n  then  prismoid bots push {name}"
        ),
    )


def _write_plugin_manifest(root: Path, name: str) -> Path:
    """Write the bundle's `plugin.json` for bot `name` under `root`; the path it wrote."""
    from prismoid.ai.plugins import format as plugin_format

    path = root / plugin_format.PLUGIN_MANIFEST
    path.write_text(plugin_format.render_plugin_manifest(name))
    return path


@bots.command("pull")
def bots_pull(
    name: str = typer.Argument(..., help="The bot to pull"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Reconstruct a bot into `./bots-work/<name>/` for editing - the inverse of `push`."""
    from prismoid.ai.bots import format as bot_format
    from prismoid.schemas.bots import BotManifest

    row = asyncio.run(_resolve(name, project))
    manifest = BotManifest.model_validate(
        {k: v for k, v in row["manifest"].items() if k not in ("cases", "golden")}
    )
    # The golden cases ride out too: a pull-edit-push round trip must keep the nightly lane's
    # rows, or the round trip silently erases them (found in review).
    cases, golden = bot_format.cases_of(row["manifest"], default_expect=name)

    root = WORK_DIR / name
    bot_dir = root / bot_format.NAMESPACE / bot_format.BOTS_DIR / name
    bot_dir.mkdir(parents=True, exist_ok=True)
    _write_plugin_manifest(root, name)
    (bot_dir / bot_format.BOT_FILE).write_text(bot_format.render_bot_md(manifest, row["instructions"]))
    (bot_dir / bot_format.CASES_FILE).write_text(bot_format.render_cases(cases, golden))
    client.emit(
        {"name": name, "version": row["version"], "path": str(root)},
        json_out,
        human=f"pulled {name} v{row['version']} → {bot_dir}",
    )


@bots.command("push")
def bots_push(
    name: str = typer.Argument(..., help="The bot to push"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Push `./bots-work/<name>/` as a new draft version, then run its selection eval.

    Push → new draft → auto-test, in one gesture: the API writes the draft, and the eval runs
    here because a selection case is a model call, and the API runs none.
    """
    from prismoid.ai.bots import format as bot_format

    root = WORK_DIR / name
    if not root.exists():
        client.fail(f"no {root} - run `prismoid bots init {name}` first", as_json=json_out)
    try:
        parsed = bot_format.parse_dir(root)
    except bot_format.BotError as exc:
        client.fail(f"{exc}", as_json=json_out)

    bot_dir = root / bot_format.NAMESPACE / bot_format.BOTS_DIR / name
    body = {
        "bot_md": (bot_dir / bot_format.BOT_FILE).read_text(),
        "cases_yaml": (bot_dir / bot_format.CASES_FILE).read_text(),
    }
    result = client.request("PUT", f"/v1/bots/{name}", body=body, params=_params(project), as_json=json_out)
    report = asyncio.run(_test(name, result["version"], project))
    if json_out:
        client.emit({"pushed": result, "selection": report}, json_out)
        return
    typer.echo(f"pushed {name} → draft v{result['version']}  ({parsed.manifest.capability.value})")
    typer.echo(_render_report(report))


# ── the gate ──────────────────────────────────────────────────────────────────────────────────


@bots.command("test")
def bots_test(
    name: str = typer.Argument(..., help="The bot to score"),
    version: int | None = typer.Option(None, "--version", help="Default: your latest draft"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run this bot's selection cases against the live roster and record the verdict.

    Measures the one thing an author controls: is the description sharp enough to win its own
    goals and refuse everyone else's? A bot that fails here would misroute in production.
    """
    report = asyncio.run(_test(name, version, project))
    client.emit(report, json_out, human=_render_report(report))
    if not report["green"]:
        raise typer.Exit(client.EXIT_ERROR)


@bots.command("publish")
def bots_publish(
    name: str = typer.Argument(..., help="The bot whose draft to publish"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Publish your latest draft - if its selection evals are green.

    A red or untested draft is a **409**, not a warning: an unpublished bot is invisible to One,
    which is exactly what should happen to one that cannot route.
    """
    result = client.request("POST", f"/v1/bots/{name}/publish", params=_params(project), as_json=json_out)
    client.emit(result, json_out, human=f"published {name} v{result['version']} - One can route to it now")


@bots.command("archive")
def bots_archive(
    name: str = typer.Argument(..., help="The bot to retire"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Retire the published head. Archived, never deleted - a task that ran it must still be
    able to say what ran."""
    result = client.request("POST", f"/v1/bots/{name}/archive", params=_params(project), as_json=json_out)
    client.emit(result, json_out, human=f"archived {name} v{result['version']}")


# ── reading ───────────────────────────────────────────────────────────────────────────────────


@bots.command("list")
def bots_list(
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Every bot in scope - the roster One routes across."""
    page = client.request("GET", "/v1/bots", params=_params(project), as_json=json_out)
    rows = page["items"] if isinstance(page, dict) else page
    lines = [
        f"  {r['name']:<24} v{r['version']:<3} {r['status']:<10} evals:{r['eval_status']:<9} {r['description'][:60]}"
        for r in rows
    ]
    client.emit(rows, json_out, human="\n".join(lines) or "no bots yet - `prismoid bots init <name>`")


@bots.command("chat")
def bots_chat(
    name: str = typer.Argument(..., help="The bot to talk to"),
    text: str = typer.Argument(..., help="What to say (prefix /task to start work outright)"),
    conversation: str | None = typer.Option(
        None, "--conversation", "-c", help="Continue this conversation id"
    ),
    new: bool = typer.Option(False, "--new", help="Open a fresh conversation"),
    task: bool = typer.Option(False, "--task", help="The explicit gesture: start this as a task"),
    wait: float = typer.Option(60.0, "--wait", help="Seconds to wait for the reply (0 = return at once)"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Talk to a bot: one message in, the door's verdict back, and the reply when it
    lands. Talk is not work - `--task` (or a `/task` prefix) is the gesture that mints one."""
    import time as _time

    params = _params(project)
    conv_id = conversation
    if conv_id is None and not new:
        rows = client.request("GET", "/v1/conversations", params={"bot": name, **params}, as_json=json_out)
        conv_id = rows[0]["id"] if rows else None
    if conv_id is None:
        body: dict[str, Any] = {"bot": name}
        if project:
            body["project_id"] = project
        conv_id = client.request("POST", "/v1/conversations", body=body, as_json=json_out)["id"]
    sent = client.request(
        "POST",
        f"/v1/conversations/{conv_id}/messages",
        body={"text": text, "start_task": task},
        as_json=json_out,
    )
    out: dict[str, Any] = {"conversation_id": conv_id, **sent, "reply": None}
    if sent["verdict"] == "answer" and wait > 0:
        deadline = _time.monotonic() + wait
        while _time.monotonic() < deadline:
            rows = client.request(
                "GET",
                f"/v1/conversations/{conv_id}/messages",
                params={"since": sent["seq"]},
                as_json=json_out,
            )
            replies = [r for r in rows if r["role"] in ("bot", "system")]
            if replies:
                out["reply"] = replies
                break
            _time.sleep(1.0)
    lines = [
        f"[{sent['verdict']}] {sent['reason']}"
        + (f" → task {sent['task_id']}" if sent.get("task_id") else "")
    ]
    for r in out["reply"] or []:
        lines.append(f"{name if r['role'] == 'bot' else 'system'}: {r['text']}")
        if r.get("proposal"):
            p = r["proposal"]
            lines.append(f"  proposal ({p['status']}): {p['goal']}  ·  start: prismoid bots chat {name} yes")
    lines.append(f"conversation {conv_id}")
    client.emit(out, json_out, human="\n".join(lines))


@bots.command("versions")
def bots_versions(
    name: str = typer.Argument(..., help="The bot name"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Every version of one bot, newest first."""
    rows = client.request("GET", f"/v1/bots/{name}/versions", params=_params(project), as_json=json_out)
    lines = [f"  v{r['version']:<3} {r['status']:<10} evals:{r['eval_status']}" for r in rows]
    client.emit(rows, json_out, human=f"{name}\n" + ("\n".join(lines) or "  no versions"))


@bots.command("diff")
def bots_diff(
    name: str = typer.Argument(..., help="The bot name"),
    from_version: int | None = typer.Option(None, "--from", help="Default: the previous version"),
    to_version: int | None = typer.Option(None, "--to", help="Default: the newest version"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """What changed between two versions - the instructions and the routing surface.

    The question "what did that edit actually do to my bot" should not require reading two blobs
    side by side, and it is the question every teaching pass raises.
    """
    rows = asyncio.run(_versions(name, project))
    if len(rows) < 2 and (from_version is None or to_version is None):
        client.fail(f"{name} has only one version - nothing to diff", as_json=json_out)
    by_version = {r["version"]: r for r in rows}
    newest = max(by_version)
    to_v = to_version or newest
    from_v = from_version or (max((v for v in by_version if v < to_v), default=to_v))
    if from_v not in by_version or to_v not in by_version:
        client.fail(f"{name}: no such version pair v{from_v}→v{to_v}", as_json=json_out)

    diff = list(
        difflib.unified_diff(
            by_version[from_v]["instructions"].splitlines(),
            by_version[to_v]["instructions"].splitlines(),
            fromfile=f"{name}@{from_v}",
            tofile=f"{name}@{to_v}",
            lineterm="",
        )
    )
    payload = {
        "name": name,
        "from": from_v,
        "to": to_v,
        "instructions_diff": diff,
        "description_changed": by_version[from_v]["description"] != by_version[to_v]["description"],
    }
    human = "\n".join(diff) or f"{name} v{from_v} → v{to_v}: instructions unchanged"
    if payload["description_changed"]:
        human += f"\n\ndescription v{to_v}:\n  {by_version[to_v]['description']}"
    client.emit(payload, json_out, human=human)


@bots.command("logs")
def bots_logs(
    name: str = typer.Argument(..., help="The bot whose runs to list"),
    limit: int = typer.Option(20, "--limit", help="How many recent runs"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """This bot's recent runs - task, outcome, spend. Observability without a dashboard.

    Your own tasks only: a bot may be shared, but a run belongs to whoever paid for it.
    """
    rows = asyncio.run(_logs(name, limit))
    lines = [
        f"  {r['state']:<10} ${r['cost_usd']:<7} task {r['task_id'][:8]}  {r['goal'][:56]}" for r in rows
    ]
    client.emit(
        rows, json_out, human=f"{name} - last {len(rows)} run(s)\n" + ("\n".join(lines) or "  none yet")
    )


@bots.command("teach")
def bots_teach(
    name: str = typer.Argument(..., help="The bot to teach"),
    from_task: str = typer.Option(..., "--from-task", help="A finished task id to distil"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Distil one finished run into this bot's next DRAFT and show what changed; teaching never publishes.
    Runs in process (a model call)."""
    try:
        result = asyncio.run(_teach(name, from_task, project))
    except PrismoidError as exc:
        # A provider hiccup or a refused digest is a sentence, never a traceback - the CLI's
        # first teach printed forty lines of pydantic-ai internals for a transient 4xx.
        client.fail(str(exc), as_json=json_out)
    if json_out:
        client.emit(result, json_out)
        return
    lesson, draft_v, diff = result["lesson"], result["version"], result["diff"]
    typer.echo(f"taught {name} → draft v{draft_v}")
    typer.echo(f"  lesson: {lesson['summary']}")
    if lesson["description"]:
        typer.echo(f"  description sharpened: {lesson['description']}")
    for case in lesson["cases"]:
        typer.echo(f"  new case: {case['goal'][:64]!r} → {case['expect']}")
    for m in lesson.get("memories", []):
        landed = "remembered" if project is None else "for you to add (shared bots take no auto-memories)"
        typer.echo(f"  {landed}: [{m['kind']}] {m['text'][:100]}")
    typer.echo("")
    typer.echo("\n".join(diff) or "  (instructions unchanged - the run taught nothing durable)")
    typer.echo(f"\nreview it, then: prismoid bots test {name} && prismoid bots publish {name}")


@bots.command("share")
def bots_share(
    name: str = typer.Argument(..., help="A PUBLISHED bot to share as a template"),
    byline: str = typer.Option(..., "--as", help="The display name shown on the preview"),
    project: str | None = _PROJECT,
    ack_urls: bool = typer.Option(False, "--ack-urls", help="Publish even though the text contains URLs"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Publish this bot as a template link - a snapshot others install in one
    click, into their own connections. Republish the same name to ship an update."""
    body: dict[str, Any] = {"name": name, "author_name": byline, "acknowledge_urls": ack_urls}
    if project:
        body["project_id"] = project
    row = client.request("POST", "/v1/templates/bots", body=body, as_json=json_out)
    client.emit(
        row,
        json_out,
        human=(
            f"shared {name} as template v{row['template_version']}\n"
            f"  slug: {row['slug']}\n"
            f"  preview (anyone, logged out): GET /v1/templates/bots/{row['slug']}\n"
            f"  web: /t/bots/{row['slug']}"
        ),
    )


@bots.command("shared")
def bots_shared(json_out: bool = typer.Option(False, "--json")) -> None:
    """Your templates - slug, version, installs."""
    rows = client.request("GET", "/v1/templates/bots", as_json=json_out)
    lines = [
        f"  {r['slug']:<28} v{r['template_version']:<3} {r['status']:<10} installs:{r['install_count']:<4} {r['name']}"
        for r in rows
    ]
    client.emit(
        rows, json_out, human="\n".join(lines) or "nothing shared yet - `prismoid bots share <name> --as you`"
    )


@bots.command("unshare")
def bots_unshare(
    slug: str = typer.Argument(..., help="The template slug (bt_...)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Unlist a template: the link 404s from now on; installed copies stand."""
    out = client.request("POST", f"/v1/templates/bots/{slug}/unpublish", as_json=json_out)
    client.emit(out, json_out, human=f"{slug} unlisted - the link is dead")


@bots.command("install")
def bots_install(
    slug: str = typer.Argument(..., help="A template slug (bt_...)"),
    name: str | None = typer.Option(None, "--name", help="Install under a different name"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """One click, as a command: the template lands as YOUR draft, wired to YOUR connections."""
    body = {"rename": name} if name else {}
    out = client.request("POST", f"/v1/templates/bots/{slug}/install", body=body, as_json=json_out)
    lines = [f"installed as {out['name']} draft v{out['version']} - test it, then publish"]
    lines += [f"  {n['kind']}: {n['detail']}" for n in out.get("notes", [])]
    if out.get("needs_connect"):
        lines.append("  connect: " + ", ".join(out["needs_connect"]) + " (on /connectors)")
    client.emit(out, json_out, human="\n".join(lines))


@bots.command("draft")
def bots_draft(
    name: str = typer.Argument(..., help="The bot to draft"),
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Expand the bot's one-sentence beginnings into a full first DRAFT (instructions, description, cases).
    Runs in process (a model call)."""
    try:
        result = asyncio.run(_draft(name, project))
    except PrismoidError as exc:
        client.fail(str(exc), as_json=json_out)
    if json_out:
        client.emit(result, json_out)
        return
    lesson, draft_v, diff = result["lesson"], result["version"], result["diff"]
    typer.echo(f"drafted {name} → draft v{draft_v}")
    typer.echo(f"  {lesson['summary']}")
    if lesson["description"]:
        typer.echo(f"  description sharpened: {lesson['description']}")
    for case in lesson["cases"]:
        typer.echo(f"  new case: {case['goal'][:64]!r} → {case['expect']}")
    typer.echo("")
    typer.echo("\n".join(diff) or "  (instructions unchanged)")
    typer.echo(f"\nreview it, then: prismoid bots test {name} && prismoid bots publish {name}")


async def _draft(name: str, project: str | None) -> dict[str, Any]:
    """Draft, then diff against the version it expanded - the `_teach` gesture without a run."""
    import difflib as _difflib

    from prismoid.db.models.bot import BotRecord
    from prismoid.db.session import session_scope
    from prismoid.operations import bot_lessons, bot_scope

    async with session_scope() as session:
        actor = await cli_actor(session)
        existing = await BotRecord.latest_draft(session, name, actor.id, project_id=_uuid(project))
        head = await BotRecord.head(session, name, project_id=_uuid(project), owner_actor_id=actor.id)
        base = bot_scope.working_version(existing, head)
        base_text = base.instructions if base is not None else ""
        base_v = base.version if base is not None else 0
        new_draft, lesson = await bot_lessons.draft(
            session, actor=actor, name=name, project_id=_uuid(project)
        )
        diff = list(
            _difflib.unified_diff(
                base_text.splitlines(),
                new_draft.instructions.splitlines(),
                fromfile=f"{name}@{base_v}",
                tofile=f"{name}@{new_draft.version}",
                lineterm="",
            )
        )
        return {
            "name": name,
            "version": new_draft.version,
            "lesson": lesson.model_dump(mode="json"),
            "diff": diff,
        }


# ── the direct-to-database half (the operator exception) ──────────────────────────────────────


async def _logs(name: str, limit: int) -> list[dict[str, Any]]:
    from prismoid.db.models.plan import PlanStep
    from prismoid.db.session import session_scope

    async with session_scope() as session:
        actor = await cli_actor(session)
        runs = await PlanStep.runs_of_agent(session, name, creator_actor_id=actor.id, limit=limit)
        return [
            {
                "task_id": str(task_id),
                "goal": goal,
                "state": step.state,
                "title": step.title,
                "attempt": step.attempt,
                "cost_usd": f"{usd:.2f}",
                "at": step.created_at.isoformat(),
            }
            for step, task_id, goal, usd in runs
        ]


async def _teach(name: str, task_id: str, project: str | None) -> dict[str, Any]:
    """Teach, then diff the result against the version it revised - one gesture, both halves."""
    import difflib as _difflib

    from prismoid.db.models.bot import BotRecord
    from prismoid.db.session import session_scope
    from prismoid.operations import bot_lessons, bot_scope

    async with session_scope() as session:
        actor = await cli_actor(session)
        draft = await BotRecord.latest_draft(session, name, actor.id, project_id=_uuid(project))
        head = await BotRecord.head(session, name, project_id=_uuid(project), owner_actor_id=actor.id)
        base = bot_scope.working_version(draft, head)  # the same base teach() revises
        base_text = base.instructions if base is not None else ""
        base_v = base.version if base is not None else 0
        draft, lesson = await bot_lessons.teach(
            session, actor=actor, name=name, task_id=uuid.UUID(task_id), project_id=_uuid(project)
        )
        diff = list(
            _difflib.unified_diff(
                base_text.splitlines(),
                draft.instructions.splitlines(),
                fromfile=f"{name}@{base_v}",
                tofile=f"{name}@{draft.version}",
                lineterm="",
            )
        )
        return {
            "name": name,
            "version": draft.version,
            "lesson": lesson.model_dump(mode="json"),
            "diff": diff,
        }


async def _test(name: str, version: int | None, project: str | None) -> dict[str, Any]:
    """The selection eval, in process. Not an API call: a case is a model call."""
    from prismoid.db.session import session_scope
    from prismoid.operations import bot_selection, bots

    async with session_scope() as session:
        actor = await cli_actor(session)
        report = await bot_selection.test_selection(
            session, actor, name, version=version, project_id=_uuid(project)
        )
        await bots.apply_eval(
            session, actor=actor, name=name, version=report.version, report=report, project_id=_uuid(project)
        )
    payload = report.model_dump(mode="json")
    payload["green"] = report.green
    return payload


async def _resolve(name: str, project: str | None) -> dict[str, Any]:
    from prismoid.db.models.bot import BotRecord
    from prismoid.db.session import session_scope

    async with session_scope() as session:
        actor = await cli_actor(session)
        row = await BotRecord.latest_draft(
            session, name, actor.id, project_id=_uuid(project)
        ) or await BotRecord.head(session, name, project_id=_uuid(project), owner_actor_id=actor.id)
        if row is None:
            raise typer.Exit(client.EXIT_ERROR)
        return {"version": row.version, "manifest": row.manifest, "instructions": row.instructions}


async def _versions(name: str, project: str | None) -> list[dict[str, Any]]:
    from prismoid.db.models.bot import BotRecord
    from prismoid.db.session import session_scope

    async with session_scope() as session:
        actor = await cli_actor(session)
        rows = await BotRecord.all_versions(session, name, project_id=_uuid(project), owner_actor_id=actor.id)
        return [
            {
                "version": r.version,
                "instructions": r.instructions,
                "description": str((r.manifest or {}).get("description", "")),
            }
            for r in rows
        ]


async def cli_actor(session: Any) -> Any:
    """Whose bots these are - and in which workspace.

    The HTTP door's own body (`api/deps.authenticate`), because the operator commands must
    resolve to the SAME identity - and bind the SAME workspace - the HTTP half
    would: `ck_` is an API key, `cs_` is a session, anything else is not a credential. Two code
    paths that disagree about who you are is how a draft becomes visible to the wrong person.
    """
    from prismoid.api.deps import authenticate, require_workspace
    from prismoid.errors import Forbidden, Unauthorized

    raw = client.current_token()
    if not raw:
        client.fail("not logged in - run `prismoid login`", client.EXIT_DENIED)
    try:
        principal = require_workspace(await authenticate(session, raw))
    except Unauthorized:
        client.fail("credential expired or unrecognised - run `prismoid login`", client.EXIT_DENIED)
    except Forbidden as refused:
        # The account is suspended, or no workspace is left to act in - the door's sentence.
        client.fail(f"{refused.message} - contact an operator", client.EXIT_DENIED)
    return principal.actor


def _uuid(raw: str | None) -> uuid.UUID | None:
    return uuid.UUID(raw) if raw else None


def _render_report(report: dict[str, Any]) -> str:
    mark = "green" if report["green"] else "RED"
    lines = [f"  selection: {report['passed']}/{report['total']} - {mark}"]
    for case in report["cases"]:
        tick = "✓" if case["ok"] else "✗"
        lines.append(f"    {tick} {case['goal'][:56]:<58} → {case['chosen']} (want {case['expected']})")
    if not report["green"]:
        lines.append("  sharpen the description until it wins its own goals and refuses the others")
    return "\n".join(lines)


_STARTER_BOT = """---
name: {name}
# The routing surface. Prismoid reads THIS and nothing else to decide whether to use your bot,
# so it must say what the bot is for and where it stops. Both halves are required.
description: >
  One line on what this bot does.
  Load when: the specific situation this bot is the right answer to.
  Not for: the neighbouring work it should never grab.
# What this role needs from a model - never a model name (that is a run-time routing decision).
# fast-executor | frontier-reasoner | long-context | cheap-classifier | code | vision
capability: fast-executor
# What it delivers. `document` and no publish grant means the system publishes the run's own
# output; any other kind needs `frame.publish` in tools.
produces: document
tools: [frame.read_file]
# skills: [extract]
# connectors: [company-drive]
---

Write the instructions here - the judgment this bot brings that a general-purpose agent does not.

Be specific about method and about what "good" looks like. Heavy reference material belongs in a
skill, not here: these instructions are loaded every time the bot runs.
"""

_STARTER_CASES = """# The publish gate. Each case is a goal and the agent that should take it.
# At least one negative case (`expect: none`) - precision is the half that is easy to lose.
selection:
  - goal: A goal squarely inside this bot's stated scope
    expect: {name}
  - goal: A neighbouring goal this bot should NOT grab
    expect: none
"""
