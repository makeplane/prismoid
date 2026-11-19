# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid skills` - the skill registry, as a command.

    prismoid skills list                 # the index the Lead plans from (published heads)
    prismoid skills versions report      # one skill's history, newest first
    prismoid skills seed                 # seed the twelve builtins at v1 (run by `just migrate`)

`list` and `versions` are thin clients of the API - they exercise the real endpoint, which is the
point of the CLI as the validation instrument. `seed` is the exception: it talks to the
database directly, because it runs as part of `just migrate` (alembic upgrade, then this) before
any process is logged in, and because a builtin's body must be put into MinIO by content hash -
something a SQL migration cannot do.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer

from prismoid.runner import client

skills = typer.Typer(
    help="Skills: the twelve builtins, plus your own. Data, not deploys.", no_args_is_help=True
)

WORK_DIR = Path("skills-work")  # where `pull` reconstructs a skill for editing (pull → edit → push)


@skills.command("list")
def skills_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """The published head of every skill - the index the Lead plans from."""
    rows = client.request("GET", "/v1/skills", as_json=json_out)
    lines = [
        f"  {r['name']:<12} v{r['version']:<3} {r['status']:<10} {r['origin']:<8} {r['description']}"
        for r in rows
    ]
    client.emit(rows, json_out, human="\n".join(lines) or "no skills - `prismoid skills seed`")


@skills.command("versions")
def skills_versions(
    name: str = typer.Argument(..., help="The skill name, e.g. report"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Every version of one skill, newest first - the history."""
    rows = client.request("GET", f"/v1/skills/{name}/versions", as_json=json_out)
    lines = [
        f"  v{r['version']:<3} {r['status']:<10} {r['origin']:<8} evals:{r['eval_status']}" for r in rows
    ]
    client.emit(rows, json_out, human=f"{name}\n" + ("\n".join(lines) or "  no versions"))


@skills.command("pull")
def skills_pull(
    name: str = typer.Argument(..., help="The skill to pull, e.g. report"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Reconstruct a skill into `./skills-work/<name>/` for editing.

    The body comes through the content-addressed loader, so the **second** pull of an unchanged skill
    is a local cache hit, not a store round-trip - the first half of the sub-60-second edit loop."""
    result = asyncio.run(_pull(name))
    if result is None:
        client.fail(f"no published skill named {name!r}", as_json=json_out)
    where = result["path"]
    how = "cache hit" if result["cache_hit"] else "fetched from store"
    client.emit(result, json_out, human=f"pulled {name}@{result['version']} → {where}/  ({how})")


async def _pull(name: str) -> dict[str, Any] | None:
    from prismoid.ai.skills.format import render, render_cases
    from prismoid.ai.skills.loader import default_loader
    from prismoid.db.models.skill import SkillRecord
    from prismoid.db.session import session_scope
    from prismoid.operations import skills as ops
    from prismoid.runner.bots_cli import cli_actor

    async with session_scope() as session:
        await cli_actor(session)  # binds the caller's workspace: skills resolve per workspace
        head = await SkillRecord.head(session, name)
        if head is None:
            return None
        # Read the cache state *before* resolving, because resolving populates it - otherwise the
        # first pull of a session would always report "cache hit" against the row it just fetched.
        cache_hit = default_loader().is_cached(head.body_sha256)
        resolved = await ops.resolve(session, name)

    skill_dir = WORK_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(render(resolved.manifest, resolved.body))
    # A whole skill directory, so `pull → edit → push` round-trips: the cases come back too, when the
    # resolved version has them (a builtin seeded before cases were persisted will not, until
    # re-seed).
    if resolved.routing_cases:
        (skill_dir / "cases.yaml").write_text(render_cases(resolved.routing_cases, resolved.golden_cases))
    # …and the bundled files: scripts/, references/, assets/ land beside SKILL.md.
    for file in resolved.files:
        target = skill_dir / file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.data)
    return {"name": name, "version": resolved.version, "path": str(skill_dir), "cache_hit": cache_hit}


@skills.command("push")
def skills_push(
    name: str = typer.Argument(..., help="The skill whose ./skills-work/<name>/ to push"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Push your edited `./skills-work/<name>/` as a new **draft** version.

    The server re-validates against the doctrine - a description missing "Load when", a body over
    budget - and answers a break with a 422 that names the field. Drafts are yours until you publish."""
    skill_dir = WORK_DIR / name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        client.fail(f"no {skill_md} - `prismoid skills pull {name}` first", as_json=json_out)
    cases = skill_dir / "cases.yaml"
    import base64

    from prismoid.ai.skills.format import SKILL_FILE_DIRS

    files = []
    for sub in SKILL_FILE_DIRS:
        root = skill_dir / sub
        if not root.is_dir():
            continue
        for file in sorted(p for p in root.rglob("*") if p.is_file()):
            files.append(
                {
                    "path": file.relative_to(skill_dir).as_posix(),
                    "content_b64": base64.b64encode(file.read_bytes()).decode(),
                }
            )
    body = {
        "skill_md": skill_md.read_text(),
        "cases_yaml": cases.read_text() if cases.is_file() else "",
        "files": files,
    }
    result = client.request("PUT", f"/v1/skills/{name}", body=body, as_json=json_out)
    version = result["version"]
    # **push → new draft → auto-test**. The API wrote the draft; the routing eval runs here in
    # the CLI, because a routing case is a model call and the API server does no model work.
    report = asyncio.run(_test(name, version))
    if json_out:
        client.emit({"pushed": result, "routing": report}, json_out)
        return
    typer.echo(f"pushed {name} → draft v{version}")
    typer.echo(_render_report(report))


@skills.command("test")
def skills_test(
    name: str = typer.Argument(..., help="The skill to test, e.g. report"),
    version: int = typer.Option(0, "--version", help="A specific version; 0 ⇒ the published head."),
    scratch: str = typer.Option(
        "", "--scratch", help="Also run a real task with this goal, pinned to the skill (e2e)."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run a skill's routing cases on the Lead (decision-only) and record the verdict; `--scratch "a goal"`
    also submits a real task pinned to this skill and prints its id."""
    report = asyncio.run(_test(name, version or None))

    if not scratch:
        client.emit(report, json_out, human=_render_report(report))
        return

    ref = f"{name}@{version}" if version else name
    accepted = client.request(
        "POST", "/v1/tasks", body={"goal": scratch, "skills": [ref], "clarify": "never"}, as_json=json_out
    )
    task_id = accepted["task_id"]
    if json_out:
        client.emit(
            {"routing": report, "scratch": {"task_id": task_id, "ref": ref, "goal": scratch}}, json_out
        )
        return
    typer.echo(_render_report(report))
    typer.echo(f"\nscratch task {task_id}  -  pinned {ref}, goal: {scratch!r}")
    typer.echo(f"  follow it: prismoid tasks events {task_id} --follow")


def _render_report(report: dict[str, Any]) -> str:
    lines = [
        f"  {'✓' if c['ok'] else '✗'} expect {c['expected']:<10} got {c['chosen'] or 'none':<10} · {c['goal'][:52]}"
        for c in report["cases"]
    ]
    verdict = "green" if report["passed"] == report["total"] and report["total"] else "red"
    head = f"{report['skill']} v{report['version']}  -  routing {verdict}: {report['passed']}/{report['total']} cases pass"
    return head + ("\n" + "\n".join(lines) if lines else "")


async def _test(name: str, version: int | None) -> dict[str, Any]:
    from prismoid.db.session import session_scope
    from prismoid.operations import skill_selection
    from prismoid.runner.bots_cli import cli_actor

    async with session_scope() as session:
        await cli_actor(session)  # binds the caller's workspace: skills resolve per workspace
        report = await skill_selection.gate(session, name, version=version)
    return report.model_dump()


@skills.command("publish")
def skills_publish(
    name: str = typer.Argument(..., help="The skill whose draft to publish"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Publish your latest draft - if its evals are green.

    A red or untested draft is a **409**, not a warning: publishing needs a green routing eval, so
    `prismoid skills test` it first. Publishing makes your draft the new head for **new** tasks;
    tasks already running keep the version they pinned."""
    result = client.request("POST", f"/v1/skills/{name}/publish", as_json=json_out)
    client.emit(result, json_out, human=f"published {name} v{result['version']} - it is now the head")


@skills.command("seed")
def skills_seed(
    force: bool = typer.Option(False, "--force", help="Drop and re-write the builtins (admin, not migrate)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Seed the twelve builtins as v1/published. Idempotent - safe on every migrate.

    `--force` re-writes the builtins from `skills/` (e.g. after a change to what a version stores). It
    touches only builtin rows, never a user's own versions."""
    n = asyncio.run(_seed(force))
    client.emit(
        {"seeded": n}, json_out, human=f"seeded {n} builtin skill(s)" if n else "builtins already seeded"
    )


async def _seed(force: bool = False) -> int:
    from prismoid.db.session import session_scope
    from prismoid.operations import skills as ops

    async with session_scope() as session:
        return await ops.seed_builtins(session, force=force)
