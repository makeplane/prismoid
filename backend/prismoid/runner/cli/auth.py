# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Session auth: `prismoid login`, `logout`, `forgot-password`, `reset-password`, `whoami`.

Top-level commands, not a group: `main.py` registers each one on the app."""

from __future__ import annotations

import typer

from prismoid.runner import client


def login(
    email: str = typer.Option(..., prompt=True, help="Your email."),
    password: str = typer.Option(..., prompt=True, hide_input=True, help="Your password."),
    signup: bool = typer.Option(False, "--signup", help="Create the account first."),
    name: str = typer.Option("", help="Display name (with --signup)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Log in and store the session token at ~/.prismoid/credentials (chmod 600)."""
    path = "/v1/auth/signup" if signup else "/v1/auth/login"
    body = {"email": email, "password": password}
    if signup:
        body["name"] = name or email.split("@")[0]

    result = client.request("POST", path, body=body, auth=False, as_json=json_out)
    client.save_token(result["token"], client.api_url())
    # The token is never echoed. It is on disk, chmod 600, and that is the only place it lives.
    client.emit(
        {"email": result["user"]["email"], "expires_at": result["expires_at"]},
        json_out,
        human=f"logged in as {result['user']['email']}",
    )


def logout(json_out: bool = typer.Option(False, "--json")) -> None:
    """Revoke this session, server-side. Not a JWT - the token is dead the moment you say so."""
    client.request("POST", "/v1/auth/logout", as_json=json_out)
    client.clear_token()
    client.emit({"ok": True}, json_out, human="logged out")


def forgot_password(
    email: str = typer.Option(..., prompt=True, help="The account's email."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Email yourself a one-time reset code. Running locally with the console email
    backend, the code prints on the API server's console."""
    client.request("POST", "/v1/auth/forgot-password", body={"email": email}, auth=False, as_json=json_out)
    client.emit(
        {"ok": True},
        json_out,
        human=(
            "if that email has an account, a reset code is on its way - it expires quickly.\n"
            "(local dev: the console email backend prints it on the API server's console)\n"
            "then: prismoid reset-password"
        ),
    )


def reset_password(
    code: str = typer.Option(..., prompt="Reset code (cr_…)", help="The code from the email."),
    password: str = typer.Option(
        ...,
        prompt="New password",
        confirmation_prompt=True,
        hide_input=True,
        help="The new password (min 12 chars).",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Set a new password with the emailed code. Every signed-in session and API key is revoked."""
    body = {"code": code.strip(), "new_password": password}
    client.request("POST", "/v1/auth/reset-password", body=body, auth=False, as_json=json_out)
    client.clear_token()  # any stored session died with the reset, server-side
    client.emit({"ok": True}, json_out, human="password reset - sign in with: prismoid login")


def whoami(json_out: bool = typer.Option(False, "--json")) -> None:
    """Who the current credentials belong to."""
    user = client.request("GET", "/v1/auth/me", as_json=json_out)
    workspace = user.get("workspace") or {}
    human = f"{user['name']} <{user['email']}>"
    if workspace:
        human += f"\nworkspace: {workspace['name']} ({workspace['slug']}, {workspace['kind']}) - {workspace['role']}"
    client.emit(user, json_out, human=human)
