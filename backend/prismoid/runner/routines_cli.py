# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid routines` - standing work, as a command (ADR-048).

    prismoid routines create "post the standup summary" --cron "0 9 * * 1-5"
    prismoid routines create "check the deploy" --every 4h --project ops
    prismoid routines create "remind me about the renewal" --at 2026-09-01T09:00:00+05:30
    prismoid routines list · show · pause · resume · retire
    prismoid routines fire ID          # Run now - the play button, present on every routine
    prismoid routines runs ID          # the fire ledger: every fire AND every non-fire, reasoned

A routine executes nothing: every fire mints an ordinary Task - watch it on the flight
recorder like any other (`prismoid tasks events <task_id> --follow`).
"""

from __future__ import annotations

from typing import Any

import typer

from prismoid.runner import client

routines = typer.Typer(
    help="Routines: standing work - a saved task plus a When. Every fire mints an ordinary Task.",
    no_args_is_help=True,
)

_PROJECT = typer.Option(None, "--project", help="The Project this routine belongs to (omit ⇒ personal)")


def _routine_line(r: dict[str, Any]) -> str:
    sched = r.get("schedule") or {}
    if sched.get("cron"):
        when = f'cron "{sched["cron"]}" {sched.get("timezone", "")}'.rstrip()
    elif sched.get("every_s"):
        when = f"every {int(sched['every_s'])}s"
    elif sched.get("at"):
        when = f"once at {sched['at']}"
    elif r.get("events"):
        when = "on " + ", ".join(r["events"].get("kinds", []))
    else:
        when = r.get("trigger_kind", "?")
    nxt = r.get("next_fire_at") or "-"
    return f"{r['id']}  {r['status']:<7} {r['name']}  · {when} · keeper {r['keeper']} · next {nxt}"


@routines.command("create")
def routines_create(
    instruction: str = typer.Argument(..., help="The standing instruction - becomes each run's goal"),
    name: str | None = typer.Option(None, "--name", help="Display name (defaults from the instruction)"),
    cron: str | None = typer.Option(None, "--cron", help='5-field cron, e.g. "0 9 * * 1-5"'),
    every: str | None = typer.Option(None, "--every", help='Interval: "30m", "4h", "2d"'),
    at: str | None = typer.Option(None, "--at", help="One-shot ISO-8601 instant (with offset)"),
    timezone: str | None = typer.Option(None, "--timezone", help="IANA tz (default: your profile's)"),
    on_event: list[str] = typer.Option(
        [],
        "--on-event",
        help="Listen for a receipt instead of a clock: task.delivered · task.failed · escalation.raised · verification.signed · artifact.produced (repeatable)",
    ),
    verdict: str | None = typer.Option(None, "--verdict", help="Narrow verification.signed: pass | fail"),
    project: str | None = _PROJECT,
    budget_usd: float | None = typer.Option(
        None, "--budget-usd", help="Per-run budget (default from the instance)"
    ),
    bot: str | None = typer.Option(
        None, "--bot", help="Keeper bot - this bot answers every run (direct lane)"
    ),
    skill: list[str] = typer.Option([], "--skill", help="Pin a skill (repeatable)"),
    connector: list[str] = typer.Option([], "--connector", help="Attach a connector (repeatable)"),
    overlap: str | None = typer.Option(None, "--overlap", help="skip (default) | queue | replace"),
    catch_up: str | None = typer.Option(None, "--catch-up", help="skip (default) | once"),
    notify: str | None = typer.Option(None, "--notify", help="always | findings (default) | failure | never"),
    hold: bool = typer.Option(False, "--hold", help="Every run mints HELD - you release it (ask-before-run)"),
    monthly_cap_usd: float | None = typer.Option(
        None, "--monthly-cap-usd", help="Auto-pause past this monthly spend"
    ),
    until: str | None = typer.Option(None, "--until", help="Season end: ISO-8601 instant (with offset)"),
    max_runs: int | None = typer.Option(None, "--max-runs", help="Season length: retire after N runs"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Create a routine and echo the resolved next fires - check them before walking away."""
    clock_dials = sum(x is not None for x in (cron, every, at))
    if on_event and clock_dials:
        client.fail("--on-event replaces the clock - drop --cron/--every/--at", as_json=json_out)
    if not on_event and clock_dials != 1:
        client.fail("pick exactly one of --cron / --every / --at (or --on-event)", as_json=json_out)
    body: dict[str, Any] = {
        "name": name or (instruction[:60] + ("…" if len(instruction) > 60 else "")),
        "instruction": instruction,
        "schedule": None if on_event else {"cron": cron, "every": every, "at": at, "timezone": timezone},
        "on_events": on_event or None,
        "event_verdict": verdict,
        "project_id": project,
        "budget_usd": budget_usd,
        "bot": bot,
        "skills": skill,
        "connectors": connector,
        "overlap": overlap,
        "catch_up": catch_up,
        "notify": notify,
        "posture": "hold" if hold else None,
        "monthly_cap_usd": monthly_cap_usd,
        "until_at": until,
        "max_runs": max_runs,
    }
    r = client.request("POST", "/v1/routines", body=body, as_json=json_out)
    upcoming = "\n".join(f"  → {t}" for t in r.get("upcoming", [])) or "  (one-shot)"
    client.emit(
        r,
        json_out,
        human=f"routine {r['id']} · {r['name']} · ${r['budget_usd_per_run']}/run\nnext fires:\n{upcoming}",
    )


@routines.command("list")
def routines_list(
    project: str | None = _PROJECT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    params: dict[str, Any] = {"project_id": project} if project else {}
    r = client.request("GET", "/v1/routines", params=params, as_json=json_out)
    items = r.get("items", [])
    human = "\n".join(_routine_line(it) for it in items) or "no routines - create one"
    client.emit(r, json_out, human=human)


@routines.command("show")
def routines_show(
    routine_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    r = client.request("GET", f"/v1/routines/{routine_id}", as_json=json_out)
    upcoming = "\n".join(f"  → {t}" for t in r.get("upcoming", []))
    lines = [
        _routine_line(r),
        f"  instruction: {r['instruction'][:200]}",
        f"  budget ${r['budget_usd_per_run']}/run · overlap {r['overlap']} · catch_up {r['catch_up']}"
        f" · notify {r['notify']} · runs {r['runs_minted']}",
    ]
    if upcoming:
        lines.append("  upcoming:\n" + upcoming)
    if r.get("pause_note"):
        lines.append(f"  paused: {r.get('paused_reason')} - {r['pause_note']}")
    client.emit(r, json_out, human="\n".join(lines))


@routines.command("set")
def routines_set(
    routine_id: str = typer.Argument(...),
    name: str | None = typer.Option(None, "--name"),
    instruction: str | None = typer.Option(None, "--instruction"),
    cron: str | None = typer.Option(None, "--cron"),
    every: str | None = typer.Option(None, "--every"),
    at: str | None = typer.Option(None, "--at"),
    timezone: str | None = typer.Option(None, "--timezone"),
    budget_usd: float | None = typer.Option(None, "--budget-usd"),
    overlap: str | None = typer.Option(None, "--overlap", help="skip | queue | replace"),
    catch_up: str | None = typer.Option(None, "--catch-up", help="skip | once"),
    notify: str | None = typer.Option(None, "--notify", help="always | findings | failure | never"),
    posture: str | None = typer.Option(None, "--posture", help="auto | hold"),
    monthly_cap_usd: float | None = typer.Option(None, "--monthly-cap-usd"),
    until: str | None = typer.Option(None, "--until"),
    max_runs: int | None = typer.Option(None, "--max-runs"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Edit a routine - changes apply from the next fire; a schedule change recomputes it now."""
    body: dict[str, Any] = {
        "name": name,
        "instruction": instruction,
        "budget_usd": budget_usd,
        "overlap": overlap,
        "catch_up": catch_up,
        "notify": notify,
        "posture": posture,
        "monthly_cap_usd": monthly_cap_usd,
        "until_at": until,
        "max_runs": max_runs,
    }
    lanes = sum(x is not None for x in (cron, every, at))
    if lanes > 1:
        client.fail("pick exactly one of --cron / --every / --at", as_json=json_out)
    if lanes or timezone is not None:
        # --timezone alone is a real edit (the server keeps the lane and moves the wall
        # clock); it must not be dropped for want of a lane flag.
        body["schedule"] = {"cron": cron, "every": every, "at": at, "timezone": timezone}
    r = client.request("PATCH", f"/v1/routines/{routine_id}", body=body, as_json=json_out)
    client.emit(r, json_out, human=_routine_line(r))


@routines.command("pause")
def routines_pause(
    routine_id: str = typer.Argument(...),
    note: str | None = typer.Option(None, "--note", help="Why (Temporal's notes field)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    r = client.request("POST", f"/v1/routines/{routine_id}/pause", body={"note": note}, as_json=json_out)
    client.emit(r, json_out, human=f"paused {r['name']}")


@routines.command("resume")
def routines_resume(
    routine_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    r = client.request("POST", f"/v1/routines/{routine_id}/resume", body={}, as_json=json_out)
    client.emit(r, json_out, human=f"resumed {r['name']} · next {r.get('next_fire_at') or '-'}")


@routines.command("retire")
def routines_retire(
    routine_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Terminal - the routine and its ledger stay readable forever; it just never fires again."""
    if not yes and not typer.confirm("Retire this routine? (its run history is kept)"):
        raise typer.Exit(0)
    r = client.request("POST", f"/v1/routines/{routine_id}/retire", body={}, as_json=json_out)
    client.emit(r, json_out, human="retired")


@routines.command("fire")
def routines_fire(
    routine_id: str = typer.Argument(...),
    data: str | None = typer.Option(
        None, "--data", help="JSON payload (or @file.json) - rides UNTRUSTED into /task/inputs/trigger.json"
    ),
    idempotency_key: str | None = typer.Option(
        None, "--idempotency-key", help="Replays with the same key return the ORIGINAL run"
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run now. A 202 whatever was decided - a refusal wears its journaled reason.

    A bare fire is the play button (policy-exempt); a fire carrying --data or a key rides the
    api source and pays the full policy stack, exactly as a token fire would."""
    import json as _json
    from pathlib import Path

    body: dict[str, Any] = {}
    if data is not None:
        raw = Path(data[1:]).read_text() if data.startswith("@") else data
        try:
            body["data"] = _json.loads(raw)
        except ValueError:
            client.fail("--data must be JSON (or @file.json)", as_json=json_out)
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    r = client.request(
        "POST", f"/v1/routines/{routine_id}/fire", body=body, headers=headers, as_json=json_out
    )
    task = f" → task {r['task_id']}" if r.get("task_id") else ""
    detail = f" ({r['detail']})" if r.get("detail") else ""
    client.emit(r, json_out, human=f"{r['decision']}{task}{detail}")


@routines.command("token")
def routines_token(
    routine_id: str = typer.Argument(...),
    revoke: bool = typer.Option(False, "--revoke", help="Close the fire door"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mint (or rotate) the routine's fire token - shown ONCE. It starts runs and nothing else:

        curl -X POST $API/v1/routines/ID/fire -H "Authorization: Bearer rt_..." \\
             -H "Content-Type: application/json" -d '{"data": {"alert": "..."}}'
    """
    if revoke:
        client.request("DELETE", f"/v1/routines/{routine_id}/token", as_json=json_out)
        client.emit({"ok": True}, json_out, human="fire door closed")
        return
    r = client.request("POST", f"/v1/routines/{routine_id}/token", body={}, as_json=json_out)
    client.emit(
        r,
        json_out,
        human=f"{r['token']}\n(shown once - rotating mints a new one; --revoke closes the door)",
    )


@routines.command("runs")
def routines_runs(
    routine_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The fire ledger - every fire and every non-fire, each wearing its reason."""
    r = client.request("GET", f"/v1/routines/{routine_id}/runs", as_json=json_out)
    lines = []
    for run in r.get("items", []):
        outcome = run.get("task_status") or "-"
        task = f" task {run['task_id']} [{outcome}]" if run.get("task_id") else ""
        detail = f" · {run['detail']}" if run.get("detail") else ""
        lines.append(f"{run['at']}  {run['source']:<6} {run['decision']:<16}{task}{detail}")
    client.emit(r, json_out, human="\n".join(lines) or "no runs yet")
