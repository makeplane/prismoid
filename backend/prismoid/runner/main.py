# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid` - the CLI.

The CLI is how the project is used, checked and demonstrated: a thin client of the routes in
`api/urls.py` (never the database). **If the CLI can't demonstrate a feature, the feature isn't
done.**

This file only assembles the app: each command group lives in its own module (`runner/cli/` and
the older `runner/*_cli.py`), registered here in the order `prismoid --help` lists it.

Every command supports `--json`. Exit codes mean things:

    0  ok
    1  error
    2  denied (a refused credential or permission); Click also exits 2 on a usage error
    3  timeout
    4  conflict (a steer issued against an older plan epoch)
"""

from __future__ import annotations

import typer

from prismoid.runner.admin_cli import admin
from prismoid.runner.bots_cli import bots
from prismoid.runner.capture_cli import capture
from prismoid.runner.channels_cli import channels as channels_cli
from prismoid.runner.cli.agents import agents
from prismoid.runner.cli.artifacts import artifacts
from prismoid.runner.cli.auth import forgot_password, login, logout, reset_password, whoami
from prismoid.runner.cli.gates import gates
from prismoid.runner.cli.inbox import inbox
from prismoid.runner.cli.keys import keys
from prismoid.runner.cli.projects import projects
from prismoid.runner.cli.run import run
from prismoid.runner.cli.tasks import do, tasks
from prismoid.runner.connectors_cli import connectors
from prismoid.runner.dev import dev
from prismoid.runner.device_cli import device
from prismoid.runner.local_cli import down, status, up
from prismoid.runner.models_cli import models
from prismoid.runner.plugins_cli import plugins
from prismoid.runner.routines_cli import routines
from prismoid.runner.skills_cli import skills
from prismoid.runner.workspaces_cli import workspaces

app = typer.Typer(
    name="prismoid",
    help="Prismoid - a digital worker that verifies before it delivers.",
    epilog="Exit codes: 0 ok · 1 error · 2 denied or a usage error · 3 timeout · 4 conflict.",
    no_args_is_help=True,
    add_completion=True,  # `prismoid --install-completion` adds tab completion to your shell
)

app.add_typer(run, name="run")
for command in (login, logout, forgot_password, reset_password, whoami):
    app.command()(command)
app.add_typer(keys, name="keys")
app.command()(do)
app.add_typer(tasks, name="tasks")
app.add_typer(gates, name="gates")
app.add_typer(projects, name="projects")
app.add_typer(capture, name="capture")
app.add_typer(artifacts, name="artifacts")
app.add_typer(inbox, name="inbox")
app.command()(agents)
app.add_typer(admin, name="admin")
app.add_typer(bots, name="bots")
app.add_typer(channels_cli, name="channels")
app.add_typer(connectors, name="connectors")
app.add_typer(dev, name="dev")
app.add_typer(device, name="device")
app.add_typer(models, name="models")
app.add_typer(workspaces, name="workspaces")
app.add_typer(plugins, name="plugins")
app.add_typer(routines, name="routines")
app.add_typer(skills, name="skills")
app.command("up")(up)
app.command("down")(down)
app.command("status")(status)


if __name__ == "__main__":
    app()
