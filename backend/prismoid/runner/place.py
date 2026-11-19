# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Where `prismoid do` sends a task: `--folder` and `--docker` onto the wire,
the current folder as the default when the deployment runs tasks on this computer, and a refusal of
a folder the person never named said as such."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from prismoid.errors import FolderRefused
from prismoid.runner import client
from prismoid.schemas.device import DOCKER_NOTE


def place_on_device(body: dict[str, Any], folder: Path | None, docker: bool, json_out: bool) -> bool:
    """`--folder` and `--docker` onto the wire. True when the folder is the current one by default."""
    defaulted = folder is None and _runs_on_device(json_out)
    if defaulted:
        folder = Path.cwd()  # like Claude Code: the folder `prismoid` ran in
    if folder is not None:
        body["folder"] = str(folder.expanduser().absolute())
    if docker:
        body["docker"] = True
        typer.echo(DOCKER_NOTE, err=True)  # stderr: `--json` stays a document
    return defaulted


def submit(body: dict[str, Any], defaulted: bool, json_out: bool) -> Any:
    """`POST /v1/tasks`. A refusal of the folder they never named says so: `prismoid do` typed in `~`."""
    try:
        return client.request("POST", "/v1/tasks", body=body, as_json=json_out, raise_invalid=defaulted)
    except client.Invalid as refused:
        told = str(refused)
        fix = ", which `prismoid` ran in: cd into a project folder, or pass --folder"
        client.fail(f"{told}{fix}" if told.startswith(FolderRefused.code) else told, as_json=json_out)


def _runs_on_device(json_out: bool) -> bool:
    """A task created now runs on this computer - the deployment says so (`/healthz`'s `device`).
    Only then does `--folder` default to the current folder: a cloud Prismoid is never sent one."""
    health = client.request("GET", "/healthz", auth=False, as_json=json_out)
    return bool(health.get("device")) if isinstance(health, dict) else False
