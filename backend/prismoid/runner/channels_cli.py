# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid channels` - a bot's external doors, as commands (ADR-056).

    prismoid channels add reviewer --kind webhook   # mint the door; the bc_ token prints ONCE
    prismoid channels list --bot reviewer
    prismoid channels log <id> --since 0            # the thread conversation's lines

The door itself is not here - it is the point: any system that can POST JSON talks to the bot
with the channel's own token, no Plane account in the loop.
"""

from __future__ import annotations

from typing import Any

import typer

from prismoid.runner import client

channels = typer.Typer(
    help="Channels: talk to a bot from outside - API, and soon email and chat.",
    no_args_is_help=True,
)


@channels.command("add")
def channels_add(
    bot: str = typer.Argument(..., help="The bot this door reaches"),
    kind: str = typer.Option(
        "webhook", "--kind", help="webhook (email/telegram arrive in their own workstreams)"
    ),
    project: str | None = typer.Option(None, "--project", help="The bot's Project (omit ⇒ personal)"),
    label: str = typer.Option("", "--label", help="A human label for the rows"),
    telegram_token: str | None = typer.Option(
        None,
        "--telegram-token",
        help="BotFather token, kind=telegram",  # gitleaks:allow (help text)
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mint one channel. The token prints ONCE - store it where the caller lives."""
    body: dict[str, Any] = {"kind": kind, "label": label}
    if telegram_token:
        body["telegram_token"] = telegram_token
    if project:
        body["project_id"] = project
    row = client.request("POST", f"/v1/bots/{bot}/channels", body=body, as_json=json_out)
    client.emit(
        row,
        json_out,
        human=(
            f"channel {row['id']} minted for {bot} ({kind})\n"
            f"  token (shown once): {row.get('token')}\n"
            f"  send:  curl -X POST .../v1/channels/{row['id']}/messages -H 'Authorization: Bearer <token>' "
            '-d \'{"message": "..."}\'\n'
            f"  read:  GET .../v1/channels/{row['id']}/messages?since=0 (same bearer)"
        ),
    )


@channels.command("list")
def channels_list(
    bot: str | None = typer.Option(None, "--bot", help="Only this bot's channels"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    path = f"/v1/bots/{bot}/channels" if bot else None
    if path is None:
        client.fail("name a bot: prismoid channels list --bot <name>", as_json=json_out)
    rows = client.request("GET", path, as_json=json_out)
    lines = [
        f"  {r['id']}  {r['kind']:<9} {r['status']:<9} {r['bot_name']:<20} cap:{r['daily_message_cap']:<5} {r['label']}"
        for r in rows
    ]
    client.emit(rows, json_out, human="\n".join(lines) or "no channels - `prismoid channels add <bot>`")


@channels.command("rotate")
def channels_rotate(
    channel_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mint a fresh token; the old one dies this instant."""
    row = client.request("POST", f"/v1/channels/{channel_id}/token", as_json=json_out)
    client.emit(row, json_out, human=f"rotated - new token (shown once): {row.get('token')}")


@channels.command("disable")
def channels_disable(
    channel_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    row = client.request("POST", f"/v1/channels/{channel_id}/disable", as_json=json_out)
    client.emit(row, json_out, human=f"{channel_id} disabled - inbound refuses honestly")


@channels.command("enable")
def channels_enable(
    channel_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    row = client.request("POST", f"/v1/channels/{channel_id}/enable", as_json=json_out)
    client.emit(row, json_out, human=f"{channel_id} active again")


@channels.command("remove")
def channels_remove(
    channel_id: str = typer.Argument(...), json_out: bool = typer.Option(False, "--json")
) -> None:
    row = client.request("DELETE", f"/v1/channels/{channel_id}", as_json=json_out)
    client.emit(row, json_out, human=f"{channel_id} removed - its conversations stay, unbound")


@channels.command("log")
def channels_log(
    channel_id: str = typer.Argument(...),
    since: int = typer.Option(0, "--since", help="Replay from this seq"),
    thread: str = typer.Option("main", "--thread", help="The channel thread (a chat id, an email thread)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The thread's conversation - what came in, what the bot said back, what the
    door decided, and every refusal's reason."""
    rows = client.request(
        "GET", f"/v1/channels/{channel_id}/log", params={"since": since, "thread": thread}, as_json=json_out
    )
    lines = [
        f"  {r['seq']:>4} {r['role']:<6} {r['verdict']:<9} {(r['sender'] or '-'):<18} "
        f"{(r['detail'] or r['text'][:60]).replace(chr(10), ' ')}"
        for r in rows
    ]
    client.emit(rows, json_out, human="\n".join(lines) or "no messages yet")


@channels.command("simulate")
def channels_simulate(
    channel_id: str = typer.Argument(..., help="An EMAIL channel id"),
    from_addr: str = typer.Option(..., "--from", help="The sender address"),
    text: str = typer.Option(..., "--text", help="The message body"),
    subject: str = typer.Option("A message for the bot", "--subject"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Deliver one inbound email locally, no relay in the loop - the dev story, and
    a direct exception to the CLI's HTTP-only rule: the same `accept_inbound` walk the provider webhook takes, so the
    allowlist, the caps, the fence, and the reply path all fire for real."""
    import asyncio as _asyncio
    import uuid as _uuid

    async def _run() -> dict[str, Any]:
        from prismoid.db.models.bot_channel import BotChannel
        from prismoid.db.session import session_scope
        from prismoid.jobs.dispatcher import ClientDispatcher
        from prismoid.operations import channels as ops
        from prismoid.operations.dispatch import set_dispatcher

        # The reply rides the worker: without a dispatcher in this process the `answer`
        # verdict's job was dropped on the floor (found in review) - enqueue-only, like the API.
        set_dispatcher(ClientDispatcher())
        async with session_scope() as session:
            row = await session.get(BotChannel, _uuid.UUID(channel_id))
            if row is None:
                raise SystemExit("no such channel")
            result = await ops.accept_inbound(
                session,
                row,
                sender=from_addr,
                body=f"{subject}\n\n{text}".strip(),
                thread_key="simulated",
                subject=subject,
            )
            return result.model_dump()

    out = _asyncio.run(_run())
    client.emit(
        out,
        json_out,
        human=f"seq {out['seq']} {out['status']}"
        + f" · {out['verdict']}"
        + (f" → task {out['task_id']}" if out.get("task_id") else f" - {out['detail']}"),
    )
