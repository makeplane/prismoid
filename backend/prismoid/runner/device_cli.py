# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid device` - the device Machine's instruments: its helper, and the
grants a person gives a task on this computer."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from typing import Any

import typer
import uvicorn

from prismoid.conf import settings
from prismoid.device.helper import private_dir
from prismoid.local.supervisor import ROLE_HELPER, ROLE_UP, Holder, InstanceLock, Process
from prismoid.runner import client
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import DOCKER_NOTE
from prismoid.schemas.device_grants import GrantKind, GrantMode, folder_key

device = typer.Typer(help="The device Machine: helper, grants.", no_args_is_help=True)
grants = typer.Typer(
    help="What tasks on this computer may reach: folders, Docker, and sites always allowed.",
    no_args_is_help=True,
)
device.add_typer(grants, name="grants")


def bind_socket(home: Path) -> socket.socket:
    """`run/helper.sock`, born 0600. Only the bind runs under a narrowed umask; the rest of the
    process keeps its own, so the folders and files the helper makes later keep their modes."""
    sock_path = private_dir(home / "run") / "helper.sock"
    sock_path.unlink(missing_ok=True)  # a stale socket from a crash
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(sock_path))
    finally:
        os.umask(old)
    sock_path.chmod(0o600)
    return sock


class _HelperServer(uvicorn.Server):
    """uvicorn runs an app's shutdown only once the requests it serves have ended, and a request here
    is a command: one still running would hold the helper until the supervisor killed it outright,
    its tree left running under nobody. So the commands stop first, and their requests end. A closing
    terminal stops a helper started by hand the same way: uvicorn alone takes SIGHUP for a crash."""

    def __init__(self, config: uvicorn.Config, halt: Callable[[], Awaitable[None]]) -> None:
        super().__init__(config)
        self.halt = halt

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        with super().capture_signals():
            previous = signal.signal(signal.SIGHUP, self.handle_exit)
            try:
                yield
            finally:
                signal.signal(signal.SIGHUP, previous)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await self.halt()
        finally:
            await super().shutdown(sockets)


def _parent() -> Holder:
    """The `prismoid up` that launched us, as its lock would name it: a parent is a proof no copied
    environment variable can fake, and its start time pins it to this very process."""
    return Holder(Process.of(os.getppid()), ROLE_UP)


@device.command("helper")
def helper(
    home: Path = typer.Option(None, "--home", help="The data home (default: PRISMOID_LOCAL_HOME)."),
) -> None:
    """Run the helper: the one process that touches this computer, on `run/helper.sock` (0600).

    One per home: a second helper would adopt the first one's frames and kill their process groups.
    So it runs only under the `prismoid up` that holds the home's lock (its parent), or holding the lock
    itself."""
    home = (home or settings.local.home).expanduser()
    lock = InstanceLock(home)
    if not lock.acquire(role=ROLE_HELPER) and (holder := lock.holder()) != _parent():
        typer.secho(
            f"error: Prismoid is already running on {home} (pid {holder.pid if holder else '?'}) with its "
            "own helper; see `prismoid status`",
            fg="red",
            err=True,
        )
        raise typer.Exit(1)
    app = create_app(home)
    server = _HelperServer(uvicorn.Config(app, log_level="warning"), app.state.halt)
    try:
        server.run(sockets=[bind_socket(home)])
    finally:
        lock.release()


def _line(grant: dict[str, Any]) -> str:
    what = (
        GrantKind.DOCKER.value
        if grant["kind"] == GrantKind.DOCKER
        else f"{grant['mode']} {grant['path']} as {grant.get('name')}"
    )
    return f"{grant['id']}  task {grant['task_id']}  {what}"


@grants.command("list")
def grants_list(
    task: str = typer.Option("", "--task", help="Only this task's grants."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Your tasks' folders and Docker grants, and the sites you always allow."""
    rows = client.request(
        "GET", "/v1/device/grants", params={"task_id": task} if task else None, as_json=json_out
    )
    sites = client.request("GET", "/v1/egress-allowances", as_json=json_out)
    lines = [_line(g) for g in rows] + [f"{s['id']}  site {s['host']}" for s in sites]
    client.emit({"grants": rows, "domains": sites}, json_out, human="\n".join(lines) or "no grants")


def _task_in(folder: Path, json_out: bool) -> str:
    """The live task working in `folder` (its read-write grant), named by the folder."""
    real = str(folder.expanduser().resolve())
    rows = client.request("GET", "/v1/device/grants", as_json=json_out)
    key = folder_key(real)  # the server's own identity of a folder: case and Unicode form aside
    found = [
        g
        for g in rows
        if g["kind"] == GrantKind.FOLDER and g["mode"] == GrantMode.RW and folder_key(g["path"]) == key
    ]
    if not found:
        client.fail(f"no task of yours works in {real}", as_json=json_out)
    return str(found[-1]["task_id"])


@grants.command("add")
def grants_add(
    kind: str = typer.Argument(..., help="folder | docker | domain"),
    value: str = typer.Argument("", help="The folder's path, or the domain."),
    *,
    task: str = typer.Option("", "--task", help="The task to grant to (folder, docker)."),
    folder: Path | None = typer.Option(None, "--folder", help="Or the task, by the folder it works in."),
    read_only: bool = typer.Option(False, "--ro", help="A folder the task may only read."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Grant a task a folder (read-write, or `--ro`) or Docker, or always allow a site. A task's
    running step takes a new grant before its next tool."""
    if kind == "domain":
        if not value:
            client.fail("name the domain: prismoid device grants add domain example.com", as_json=json_out)
        row = client.request("POST", "/v1/egress-allowances", body={"host": value}, as_json=json_out)
        client.emit(row, json_out, human=f"always allowed: {row['host']} ({row['id']})")
        return
    if kind not in GrantKind:
        client.fail(f"unknown grant {kind!r}: folder | docker | domain", as_json=json_out)
    if task:
        task_id = task
    elif folder is not None:
        task_id = _task_in(folder, json_out)
    else:
        client.fail("name the task: --task ID, or --folder PATH (the folder it works in)", as_json=json_out)
    body: dict[str, Any] = {"task_id": task_id, "kind": kind}
    if kind == GrantKind.FOLDER:
        if not value:
            client.fail("name the folder: prismoid device grants add folder PATH --task ID", as_json=json_out)
        body |= {
            "path": str(Path(value).expanduser().absolute()),
            "mode": GrantMode.RO if read_only else GrantMode.RW,
        }
    row = client.request("POST", "/v1/device/grants", body=body, as_json=json_out)
    note = f"\n  {DOCKER_NOTE}" if kind == GrantKind.DOCKER else ""
    client.emit(row, json_out, human=f"granted: {_line(row)}{note}")


@grants.command("revoke")
def grants_revoke(
    grant_id: str = typer.Argument(..., help="The grant's id (from `prismoid device grants list`)."),
    domain: bool = typer.Option(False, "--domain", help="It is an always-allowed site."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Revoke a grant. A running step loses it before its next tool, though a command already
    running finishes; a site asks again."""
    path = f"/v1/egress-allowances/{grant_id}" if domain else f"/v1/device/grants/{grant_id}"
    client.request("DELETE", path, as_json=json_out)
    client.emit({"ok": True}, json_out, human="revoked")
