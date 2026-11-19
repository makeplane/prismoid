# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid run api|worker` - the two run modes of the one image."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import cast

import typer

from prismoid.conf import settings

run = typer.Typer(help="The two run modes of the one image: the API and the worker.", no_args_is_help=True)


@run.command("api")
def run_api(
    host: str = typer.Option(settings.api.host, help="Bind address."),
    port: int = typer.Option(settings.api.port, help="Bind port."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on source changes (dev)."),
) -> None:
    """Serve the HTTP API."""
    import uvicorn

    # uvloop + httptools are the performance floor; uvicorn[standard] picks them automatically.
    uvicorn.run(
        "prismoid.api.app:app",
        host=host,
        port=port,
        reload=reload,
        access_log=False,  # Logfire owns the request span; a second access log is noise
        # Behind a reverse proxy, set PRISMOID_API_FORWARDED_ALLOW_IPS so X-Forwarded-For is
        # honored and the per-IP rate limits see real clients, not the proxy.
        forwarded_allow_ips=settings.api.forwarded_allow_ips or None,
        # Screen frames are JPEGs: deflating each one for each viewer costs the event loop and saves nothing.
        ws_per_message_deflate=False,
    )


@run.command("worker")
def run_worker() -> None:
    """Run the DBOS worker - the process that owns all machine work; the API runs none."""
    # Loaded only here: the worker module pulls in the database engine and every ORM model, which
    # would add a seventh of a second to every other `prismoid` command (the CLI never touches the
    # database).
    worker_main = cast(Callable[[], None], importlib.import_module("prismoid.runner.worker").main)
    worker_main()
