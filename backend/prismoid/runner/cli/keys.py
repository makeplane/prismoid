# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid keys create|list|revoke` - programmatic API keys, shown once and hashed at rest."""

from __future__ import annotations

import typer

from prismoid.runner import client

keys = typer.Typer(help="Programmatic keys. Shown once; hashed at rest.", no_args_is_help=True)


@keys.command("create")
def keys_create(
    name: str = typer.Argument(..., help="What this key is for."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Create a key. The plaintext is printed once and never again - store it now."""
    key = client.request("POST", "/v1/auth/api-keys", body={"name": name}, as_json=json_out)
    client.emit(
        key, json_out, human=f"{key['key']}\n\nworkspace: {key['workspace_id']}\n(shown once - store it now)"
    )


@keys.command("list")
def keys_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """List keys. The keys themselves are not listed, because we do not have them."""
    rows = client.request("GET", "/v1/auth/api-keys", as_json=json_out)
    lines = [f"{r['id']}  {r['name']:<20}  {'revoked' if r['revoked_at'] else 'active'}" for r in rows]
    client.emit(rows, json_out, human="\n".join(lines) or "no keys")


@keys.command("revoke")
def keys_revoke(
    key_id: str = typer.Argument(..., help="The key id (not the key)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Revoke a key. It stops working on the next request - no waiting for an expiry."""
    client.request("DELETE", f"/v1/auth/api-keys/{key_id}", as_json=json_out)
    client.emit({"ok": True}, json_out, human="revoked")
