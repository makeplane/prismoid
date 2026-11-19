# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid agents` - the built-in roster. A top-level command; `main.py` registers it."""

from __future__ import annotations

import typer

from prismoid.runner import client


def agents(json_out: bool = typer.Option(False, "--json")) -> None:
    """The roster. Agents are roles - "writer", "maker" - never models."""
    rows = client.request("GET", "/v1/agents", as_json=json_out)
    lines = [f"{r['name']:<12} {r['capability']:<18} {r['description']}" for r in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no agents")
