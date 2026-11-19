# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The CLI's HTTP client. A thin client of `api/urls.py` - **it never touches the database**.

That constraint is not architectural purity for its own sake. The CLI is the instrument every
milestone's real-world validation runs through, so if it could reach around the API and read
Postgres directly, a validation could pass against a system whose *API* was broken - and the
validation would be worthless. It speaks HTTP, exactly like every other client.

**The one honest exception is the OPERATOR commands** (`models seed|import|doctor` and the `dev`
group): those open a database session directly, because seeding a catalog and diagnosing the
ledger are DBA tasks that predate - or bypass - any API. Everything a normal user does is HTTP.

Exit codes mean things: **0** success · **1** error · **2** denied/authz · **3** timeout ·
**4** conflict (a stale steer).
"""

from __future__ import annotations

import json as jsonlib
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, NoReturn

import httpx
import typer

from prismoid.conf import settings

CREDENTIALS = Path.home() / ".prismoid" / "credentials"
# This terminal's connection to a screen, kept beside the credentials: a claim and a
# `--shot` are one connection, so the shot is the holder's own under a private lease.
SCREEN_CONN = CREDENTIALS.parent / "screen-conn"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DENIED = 2
EXIT_TIMEOUT = 3
EXIT_CONFLICT = 4


# ── the stored session ────────────────────────────────────────────────────────────────────────


def screen_conn() -> str:
    """This terminal's screen connection id, minted at first use and kept owner-only. Not a
    secret - a lease binds a person AND a connection - but the CLI's claim and its `--shot` must
    name the same one: the mask hides a private screen from every connection but the holder's."""
    try:
        kept = SCREEN_CONN.read_text().strip()
    except OSError:
        kept = ""
    if re.fullmatch(r"[0-9a-f]{16}", kept):
        return kept
    conn = secrets.token_hex(8)
    SCREEN_CONN.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SCREEN_CONN.write_text(conn)
    SCREEN_CONN.chmod(0o600)
    return conn


# The credentials file keeps one token per API address (`tokens`), and which address is in use
# (`active`): a token is sent only to the server that minted it, and signing in to a second Prismoid
# (`prismoid up` on this Mac beside a dev stack) never throws the first one's away. The single pair of
# earlier versions (`token`, `api_url`) is kept beside the map for the CLIs that still read only it
# (another worktree's): written when it is absent or names the address being saved, else left alone.


def _raw() -> dict[str, Any]:
    try:
        data = jsonlib.loads(CREDENTIALS.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_url(data: dict[str, Any]) -> str | None:
    """The address the earlier single pair names, when there is one."""
    if not data.get("token"):
        return None
    return _norm(str(data.get("api_url") or settings.cli.api_url))


def _token_map(data: dict[str, Any]) -> dict[str, str]:
    tokens = data.get("tokens")
    return {_norm(str(k)): str(v) for k, v in tokens.items() if v} if isinstance(tokens, dict) else {}


def _credentials() -> tuple[str | None, dict[str, str]]:
    """(the active address, every address's token) - the earlier pair included."""
    data = _raw()
    tokens = _token_map(data)
    legacy = _legacy_url(data)
    if legacy is not None and legacy not in tokens:
        tokens[legacy] = str(data["token"])
    active = data.get("active")
    return (_norm(active) if isinstance(active, str) and active else legacy), tokens


def _norm(url: str) -> str:
    return url.rstrip("/")


def _write_credentials(data: dict[str, Any]) -> None:
    """Every server's token in one file, so it is replaced whole or not at all: a temp file born
    `0600` (never a moment wider), synced, renamed over. The directory is `0700`. A token in a
    world-readable file is a token you gave away."""
    CREDENTIALS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CREDENTIALS.with_name(f".{CREDENTIALS.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(jsonlib.dumps(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CREDENTIALS)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_token(token: str, api_url: str) -> None:
    """Keep `token` for `api_url` and make that address the one in use."""
    url = _norm(api_url)
    data = _raw()
    tokens = _token_map(data)
    legacy = _legacy_url(data)
    if legacy is not None and legacy not in tokens:
        tokens[legacy] = str(data["token"])  # into the map too, so the pair can move on later
    tokens[url] = token
    data["tokens"], data["active"] = tokens, url
    if legacy is None or legacy == url:
        data["token"], data["api_url"] = token, url
    _write_credentials(data)


def clear_token() -> None:
    """Forget the token of the address in use; the others stay (the earlier pair too, unless it
    names this address)."""
    url = _norm(api_url())
    data = _raw()
    tokens = _token_map(data)
    dropped = tokens.pop(url, None) is not None
    if _legacy_url(data) == url:
        data.pop("token", None)
        data.pop("api_url", None)
        dropped = True
    if not dropped:
        return
    data["tokens"] = tokens
    if tokens or data.get("token"):
        _write_credentials(data)
    else:
        CREDENTIALS.unlink(missing_ok=True)


def token_for(url: str) -> str | None:
    return _credentials()[1].get(_norm(url))


def stored_token() -> str | None:
    """The stored token of the address in use - never another server's."""
    return token_for(api_url())


def current_token() -> str | None:
    """`$PRISMOID_API_KEY` beats the stored session - scripts and CI should not depend on whoever
    last ran `prismoid login` on the box."""
    return os.environ.get("PRISMOID_API_KEY") or settings.cli.api_key.get_secret_value() or stored_token()


def api_url() -> str:
    """`$PRISMOID_CLI_API_URL` - from the environment, `.env` or `~/.prismoid/config` - when one is set;
    else the address last signed in to (`prismoid login`, `prismoid up`'s first run); else the built-in
    default. A configured address is a choice someone made; the stored one only beats the default."""
    if os.environ.get("PRISMOID_CLI_API_URL"):
        return os.environ["PRISMOID_CLI_API_URL"]
    if "api_url" in settings.cli.model_fields_set:
        return settings.cli.api_url
    return _credentials()[0] or settings.cli.api_url


# ── output ────────────────────────────────────────────────────────────────────────────────────


def emit(data: Any, as_json: bool, human: str | None = None) -> None:
    """Every command supports `--json`. Machines read the JSON; humans read the prose."""
    if as_json:
        typer.echo(jsonlib.dumps(data, indent=2, default=str))
    elif human is not None:
        typer.echo(human)
    else:
        typer.echo(jsonlib.dumps(data, indent=2, default=str))


def fail(message: str, code: int = EXIT_ERROR, as_json: bool = False) -> NoReturn:
    if as_json:
        typer.echo(jsonlib.dumps({"error": message}), err=True)
    else:
        typer.secho(f"error: {message}", fg="red", err=True)
    raise typer.Exit(code)


# ── requests ──────────────────────────────────────────────────────────────────────────────────


class Invalid(Exception):
    """A 422 the caller asked to handle itself - see `request(raise_invalid=True)`."""


class Conflicted(Exception):
    """A 409 the caller asked to handle itself (`raise_conflict=True`): `error` is the envelope's
    body, extras included (a stale steer's `missed`)."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message", "")))
        self.error = error


class Refused(Exception):
    """An error response the caller asked to handle itself - see `request(raise_refused=True)`.
    Carries the envelope's message and the exit code `fail` would have used."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _exit_code_for(status: int) -> int:
    if status in (401, 403):
        return EXIT_DENIED
    return EXIT_ERROR


def request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    auth: bool = True,
    as_json: bool = False,
    timeout: float = 30.0,
    raise_invalid: bool = False,
    raise_conflict: bool = False,
    raise_refused: bool = False,
    headers: dict[str, str] | None = None,
) -> Any:
    """One request. Maps the API's error envelope onto a message and an exit code.

    `raise_refused` hands any error response - and a timeout or a refused connection - back as
    `Refused` instead of ending the command - for
    a second call whose failure must not hide what the first one already did (B3's raise, then its
    resume). `raise_conflict` hands a 409 back as `Conflicted`, its body intact (a stale steer's
    `missed`); it is checked first. `headers` extends (never replaces) the auth header - the
    Idempotency-Key lane."""
    headers = dict(headers or {})
    if auth:
        token = current_token()
        if not token:
            fail("not logged in - run `prismoid login`", EXIT_DENIED, as_json)
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = httpx.request(
            method,
            f"{api_url()}{path}",
            json=body,
            params=params,
            headers=headers,
            timeout=timeout,
            # httpx does NOT follow redirects by default. Without this, FastAPI's trailing-slash
            # 307 sails straight through the `>= 400` check as a "success" with an empty body, and
            # the caller gets `None` and a TypeError three frames later. The bug reads as a
            # mysterious crash and is really a redirect nobody followed.
            follow_redirects=True,
        )
    except httpx.TimeoutException:
        if raise_refused:
            raise Refused(f"timed out talking to {api_url()}", EXIT_TIMEOUT) from None
        fail(f"timed out talking to {api_url()}", EXIT_TIMEOUT, as_json)
    except httpx.ConnectError:
        if raise_refused:
            raise Refused(f"cannot reach {api_url()}", EXIT_ERROR) from None
        fail(f"cannot reach {api_url()} - is the API running? (`just api`)", EXIT_ERROR, as_json)

    if response.status_code >= 400:
        err: Any = None
        try:
            err = response.json()["error"]
            message = f"{err['code']}: {err['message']}"
        except (ValueError, KeyError, TypeError):
            message = f"http {response.status_code}: {response.text[:200]}"

        # **A 422 is recoverable, and one caller wants to recover from it.** Every other error ends
        # the command; a rejected *answer* does not - the server said which question was wrong, and
        # the human is two keystrokes from fixing it. Exiting there would throw away a task they have
        # already paid a model call for, over a typo.
        if raise_invalid and response.status_code == 422:
            raise Invalid(message)
        # A 409 a caller can act on: a stale steer names what its author missed.
        if raise_conflict and response.status_code == 409:
            raise Conflicted(err if isinstance(err, dict) else {"message": message})
        if raise_refused:
            raise Refused(message, _exit_code_for(response.status_code))

        fail(message, _exit_code_for(response.status_code), as_json)

    if not response.content:
        return None
    return response.json()


def stream_sse(path: str, *, params: dict[str, Any] | None = None, timeout: float = 300.0):
    """The terminal flight recorder. Yields raw SSE frames.

    The CLI is not a browser, so it uses the `Authorization` header rather than `?token=` -
    both paths land in the same `require_actor`.
    """
    token = current_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    with httpx.stream(
        "GET", f"{api_url()}{path}", params=params, headers=headers, timeout=timeout
    ) as response:
        if response.status_code >= 400:
            response.read()
            typer.secho(f"error: stream refused ({response.status_code})", fg="red", err=True)
            sys.exit(_exit_code_for(response.status_code))
        yield from response.iter_lines()
