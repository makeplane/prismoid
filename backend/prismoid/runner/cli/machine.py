# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks machine` and `prismoid tasks screen`: the task's Machine, who drives its browser, and
one frame of its screen saved from a terminal. `tasks.py` registers both on the `tasks` group."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit

import typer
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException
from websockets.sync.client import ClientConnection, connect

from prismoid.runner import client
from prismoid.runner.stream import person_name, printable
from prismoid.schemas.screen import NO_SCREEN, NO_SCREEN_TEXT, WireError, decode, encode

MACHINE_WORDS = {
    "waking": "Running",
    "running": "Running",
    "parked": "Waiting",
    "idle": "Idle",
    "sleeping": "Asleep",
    "asleep": "Asleep",
    "gone": "Gone",
}


def _machine(m: dict[str, Any]) -> str:
    minutes = round(int(m.get("warm_seconds") or 0) / 60)
    lines = [
        f"  machine:  {MACHINE_WORDS.get(str(m['state']), m['state'])} since {m['state_since']} · {m['profile']}",
        f"  time:     {minutes} min · ${m.get('cost_usd', '0')}",
    ]
    if m.get("checkpoint_bytes"):
        saved = f"  saved:    {int(m['checkpoint_bytes']) // (1024 * 1024)} MB of files"
        lines.append(saved + (" and browser logins" if m.get("browser_saved") else ""))
    return "\n".join(lines)


def tasks_machine(
    task_id: str = typer.Argument(...),
    sleep: bool = typer.Option(False, "--sleep", help="Put the Machine to sleep now"),
    reset: bool = typer.Option(False, "--reset", help="Delete the Machine's files and browser logins"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The task's Machine: its state, or put it to sleep, or reset it."""
    if sleep and reset:
        client.fail("give --sleep or --reset, not both", as_json=json_out)
    if sleep:
        r = client.request("POST", f"/v1/tasks/{task_id}/machine/sleep", as_json=json_out)
        client.emit(r, json_out, human=_machine(r))
        return
    if reset:
        if not yes and json_out:  # a script never meets a prompt on stdout
            client.fail("--reset with --json needs --yes: it deletes the Machine's files", as_json=True)
        if not yes and not typer.confirm("Reset the Machine? Its files and browser logins are deleted."):
            raise typer.Exit(0)
        r = client.request("POST", f"/v1/tasks/{task_id}/machine/reset", as_json=json_out)
        client.emit(r, json_out, human=_machine(r))
        return
    task = client.request("GET", f"/v1/tasks/{task_id}", as_json=json_out)
    machine = task.get("machine")
    client.emit(machine, json_out, human=_machine(machine) if machine else "this task has no Machine yet")


def tasks_screen(
    task_id: str = typer.Argument(...),
    claim: bool = typer.Option(False, "--claim", help="Take control of the Machine's browser"),
    private: bool = typer.Option(False, "--private", help="With --claim: nobody else sees the screen"),
    renew: bool = typer.Option(
        False, "--renew", help="Renew your control: private, 10 more minutes; otherwise another minute"
    ),
    release: bool = typer.Option(False, "--release", help="Hand back control"),
    end: bool = typer.Option(False, "--end", help="End someone's control (the Project owner)"),
    shot: Path | None = typer.Option(None, "--shot", help="Save one frame of the screen as a JPEG"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The task's screen: who drives the browser, take or hand back control, or save a frame."""
    picked = [flag for flag, on in _screen_flags(claim, renew, release, end, shot) if on]
    if len(picked) > 1:
        client.fail(f"give one of {', '.join(picked)}, not several", as_json=json_out)
    if private and not claim:
        client.fail("--private goes with --claim", as_json=json_out)
    base = f"/v1/tasks/{task_id}/screen"
    if claim:
        # This terminal's connection (its `--shot` is the holder's own under a private lease). The
        # CLI never sends input, so its lease runs out on silence (60 s) or, private, at the
        # 10-minute cap - unless it hands back first.
        body = {"conn": client.screen_conn(), "private": private}
        r = client.request("POST", f"{base}/control", body=body, as_json=json_out)
        human = _control_line(r)
        if not private:
            human += "\n  non-private control ends after a minute without input; use --private or --renew"
        client.emit(r, json_out, human=human)
    elif renew:
        r = client.request("POST", f"{base}/control/renew", as_json=json_out)
        client.emit(r, json_out, human=_control_line(r))
    elif release or end:
        # One route: the holder releases, the Project owner ends anyone's (the server decides).
        client.request("DELETE", f"{base}/control", as_json=json_out)
        client.emit({"control": None}, json_out, human="control ended" if end else "handed back control")
    elif shot is not None:
        out = _screen_shot(task_id, shot, json_out)
        client.emit(
            out,
            json_out,
            human=(f"saved a {out['w']}x{out['h']} frame" if out["w"] and out["h"] else "saved a frame")
            + f" to {out['path']} ({out['bytes']} bytes)",
        )
    else:
        task = client.request("GET", f"/v1/tasks/{task_id}", as_json=json_out)
        machine = task.get("machine") or {}
        view = {"state": machine.get("state"), "control": machine.get("control")}
        client.emit(view, json_out, human=_screen_lines(view))


def _screen_flags(
    claim: bool, renew: bool, release: bool, end: bool, shot: Path | None
) -> list[tuple[str, bool]]:
    return [
        ("--claim", claim),
        ("--renew", renew),
        ("--release", release),
        ("--end", end),
        ("--shot", shot is not None),
    ]


def _control_line(c: dict[str, Any]) -> str:
    kind = "private control" if c.get("private") else "control"
    return f"you have {kind} of the browser until {printable(str(c['until']))}"


def _screen_lines(view: dict[str, Any]) -> str:
    if view["state"] is None:
        return "this task has no Machine yet"
    c = view["control"]
    who = (
        f"{person_name(c['holder']) or 'someone'} is driving{' (private)' if c.get('private') else ''}"
        f" until {printable(str(c['until']))}"
        if c
        else "nobody; the agent has the browser"
    )
    return f"  machine:  {MACHINE_WORDS.get(str(view['state']), view['state'])}\n  control:  {who}"


SHOT_TIMEOUT_S = 15.0
MAX_MESSAGE = 16 * 1024 * 1024  # four times the screen's 4 MiB cap: a guard, not a contract
# The screen socket's close codes -> what the terminal says, and its exit code.
_SCREEN_CLOSES = {
    4401: ("the screen refused the ticket or the credential", client.EXIT_DENIED),
    4404: ("no such task or Machine", client.EXIT_ERROR),
    4408: ("the screen closed: too slow", client.EXIT_ERROR),
    4410: ("Computer use is off.", client.EXIT_ERROR),
}
# What can go wrong on the socket: the library's own errors, the network's, and silence.
_SOCKET_ERRORS = (WebSocketException, OSError)


def _screen_shot(task_id: str, path: Path, json_out: bool) -> dict[str, Any]:
    """Mint a ticket, open the screen socket, save the first frame, close: the whole pipe from a
    terminal. A masked or sleeping screen, or a Machine with none, has no frame to give, and says
    so."""
    url = _screen_url(task_id, json_out)
    try:
        # No compression, proxy or keepalive pings: one short read on a socket the API answers.
        conn = connect(
            url,
            open_timeout=SHOT_TIMEOUT_S,
            close_timeout=2.0,
            ping_interval=None,
            max_size=MAX_MESSAGE,
            compression=None,
            proxy=None,
        )
    except _SOCKET_ERRORS as e:
        _screen_failed(e, json_out)
    try:
        conn.send(encode({"t": "hello", "v": 1}))
        conn.send(encode({"t": "hint", "fps": 1, "refresh": True}))
        header, payload = _first_frame(conn, json_out)
    except _SOCKET_ERRORS as e:
        _screen_failed(e, json_out)
    except WireError as e:
        client.fail(f"the screen sent a malformed message: {printable(str(e))}", as_json=json_out)
    finally:
        conn.close()
    if header.get("fmt") != "jpeg" or not payload:
        client.fail("the screen sent a frame that is not a JPEG", as_json=json_out)
    try:
        path.write_bytes(payload)
    except OSError as e:
        client.fail(f"cannot write {path}: {e.strerror or 'error'}", as_json=json_out)
    return {"path": str(path), "bytes": len(payload), "w": header.get("w"), "h": header.get("h")}


def _screen_url(task_id: str, json_out: bool) -> str:
    """The screen socket's URL, carrying a freshly minted single-use ticket (never printed)."""
    base = client.api_url().rstrip("/")
    if urlsplit(base).scheme not in ("http", "https"):
        client.fail("the API URL must be an http or https URL", as_json=json_out)
    issued = client.request(
        "POST", f"/v1/tasks/{task_id}/screen/ticket", body={"conn": client.screen_conn()}, as_json=json_out
    )
    ticket = quote(issued["ticket"], safe="")
    return "ws" + base.removeprefix("http") + f"/v1/tasks/{task_id}/screen?ticket={ticket}"


def _first_frame(conn: ClientConnection, json_out: bool) -> tuple[dict[str, Any], bytes]:
    """The first frame message within the shot's deadline; a status that means none will come
    ends the command with its reason. Text messages are not the screen's and are skipped."""
    deadline = time.monotonic() + SHOT_TIMEOUT_S
    while (left := deadline - time.monotonic()) > 0:
        data = conn.recv(timeout=left)
        if not isinstance(data, bytes):
            continue
        header, payload = decode(data)
        if header.get("t") == "frame":
            return header, payload
        _no_frame_coming(header, json_out)
    client.fail(f"no frame arrived in {SHOT_TIMEOUT_S:.0f} s", client.EXIT_TIMEOUT, json_out)


def _no_frame_coming(header: dict[str, Any], json_out: bool) -> None:
    """Fail when `header` says the screen will send no frame: a private sign-in masks it, the
    Machine is asleep, or it has no screen. Any other message is not news."""
    kind, upstream = header.get("t"), header.get("upstream")
    if kind == "masked" and header.get("holder") is not None:  # `holder: null` is no mask
        holder = header.get("holder")
        name = person_name(holder) if isinstance(holder, dict) else printable(str(holder or ""))[:60]
        client.fail(f"{name or 'someone'} is signing in privately", as_json=json_out)
    if kind == "status" and upstream == "asleep":
        client.fail("The Machine is asleep.", as_json=json_out)
    if kind == "status" and upstream == NO_SCREEN:
        client.fail(NO_SCREEN_TEXT, as_json=json_out)


def _screen_failed(e: Exception, json_out: bool) -> NoReturn:
    """The socket's failure as the terminal says it. A message never carries the URL: it holds
    the ticket."""
    if isinstance(e, TimeoutError):
        client.fail("timed out waiting for the server", client.EXIT_TIMEOUT, json_out)
    if isinstance(e, ConnectionClosed):
        _closed(e, json_out)
    if isinstance(e, InvalidStatus):
        status = e.response.status_code
        if status in (401, 403):
            client.fail(f"the screen refused the connection (http {status})", client.EXIT_DENIED, json_out)
        client.fail(f"the server refused the WebSocket (http {status})", as_json=json_out)
    if isinstance(e, InvalidURI):
        client.fail("not a WebSocket URL (the scheme must be ws or wss)", as_json=json_out)
    if isinstance(e, OSError):
        client.fail(f"cannot reach the screen: {printable(e.strerror or type(e).__name__)}", as_json=json_out)
    client.fail(f"the WebSocket handshake failed: {printable(str(e))}", as_json=json_out)


def _closed(e: ConnectionClosed, json_out: bool) -> NoReturn:
    if e.rcvd is None:
        client.fail("the server dropped the connection without a close frame", as_json=json_out)
    code, reason = e.rcvd.code, e.rcvd.reason
    if code in _SCREEN_CLOSES:
        message, exit_code = _SCREEN_CLOSES[code]
        client.fail(message, exit_code, json_out)
    said = f": {reason}" if reason else ""
    client.fail(printable(f"closed by the server ({code}{said})"), as_json=json_out)
