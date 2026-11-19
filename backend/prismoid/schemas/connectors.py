# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Connectors - the values, and the deterministic half of the OAuth client (ADR-020).

A connector is a user's connection to a remote MCP server; what crosses into the engine is the
frozen `ConnectorSpec` below - one secret field, masked everywhere. Everything else in this
file is a **pure function** (the same split `ai/context/retry.py`
lives by): PKCE, discovery-URL derivation, authorize-URL and token-request assembly, the SSRF
door check. `operations/connectors.py` does the httpx choreography and the rows; nothing here
touches the network, the clock, or a table - so all of it unit-tests in microseconds.

Deliberately imports nothing from the `mcp` SDK: operations parses wire metadata with the
SDK's typed models and hands *primitives* down. Two definitions of a wire shape drift; one
consumer of it doesn't.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, cast
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# The SSRF door moved to `schemas/egress.py` - the fetch tool needed the same
# literal refusals, and two copies of an SSRF rule drift into a hole. Re-exported so this
# module's public API (what operations and the tests import) is unchanged.
from prismoid.schemas.egress import (
    check_server_url as check_server_url,
)
from prismoid.schemas.egress import (
    is_localhost as is_localhost,
)
from prismoid.schemas.ids import ConnectorId
from prismoid.schemas.inputs import visible

# ── the vocabulary ────────────────────────────────────────────────────────────────────────────


class ConnectorStatus(StrEnum):
    """The row's lifecycle. `pending_auth` = created, awaiting the OAuth dance; `authorizing` =
    a flow is in flight (state minted, browser out); `live` = probed and usable; `needs_reauth`
    = tokens died (refresh failed or a mount saw 401); `error` = the server itself refused the
    probe; `disabled` = the owner switched it off without forgetting it."""

    PENDING_AUTH = "pending_auth"
    AUTHORIZING = "authorizing"
    LIVE = "live"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"
    DISABLED = "disabled"


class AuthKind(StrEnum):
    NONE = "none"  # the server answered an anonymous initialize
    BEARER = "bearer"  # a static token the user pasted
    OAUTH = "oauth"  # the full authorization-code dance


class ToolPolicy(StrEnum):
    """What one tool of one connector may do. `allow` runs; `ask` pauses the step on
    an approve gate the owner resolves; `off` never enters the inventory the model sees."""

    ALLOW = "allow"
    ASK = "ask"
    OFF = "off"


ATTACHABLE: Final = frozenset({ConnectorStatus.LIVE})  # what a task may attach at the door


class ConnectorToolInfo(BaseModel):
    """One row of the cached inventory - enough for the composer, the brief, and the grant.

    `read_only`/`destructive` mirror the server's MCP ToolAnnotations (`readOnlyHint`,
    `destructiveHint`) as captured at probe time; `None` means the server declared nothing.
    Hints from an untrusted server pick DEFAULTS (below), never bypass an owner's override.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    read_only: bool | None = None
    destructive: bool | None = None


class ConnectorSpec(BaseModel):
    """What the engine is handed, and nothing more (the `TaskDeps` law, applied to a secret).

    ONE secret field: `authorization` is the full header value ("Bearer eyJ…"), a `SecretStr`
    so `repr()`, `str()` and `model_dump()` all mask it - a stray `print(deps)` or a captured
    span cannot leak a token. Bearer and OAuth connectors collapse to this same shape on
    purpose: the engine never learns which dance produced the header, so it can never treat
    them differently.

    `tool_policies` carries the EFFECTIVE policy per tool, already derived by
    operations - only `allow` and `ask` ever cross: an `off` tool's name never enters
    `tool_names`, so for the engine it does not exist.
    """

    model_config = ConfigDict(frozen=True)

    id: ConnectorId
    slug: str  # the tool prefix - `gmail` ⇒ `gmail_send_email`; stable across park/resume
    name: str  # the display name, for prompts and journals
    url: str  # the MCP endpoint itself
    tool_names: tuple[str, ...] = ()  # the probed inventory minus `off` - all that mounts
    instructions: str = ""  # server instructions, sanitized + capped at probe time
    authorization: SecretStr | None = None
    tool_policies: dict[str, ToolPolicy] = Field(default_factory=dict)


class ServiceReach(BaseModel):
    """What a connected service does beyond the tools a run mounted: the tools a planned step can call
    (asking the person first where the approval rules say so) and the ones its owner turned off."""

    model_config = ConfigDict(frozen=True)

    name: str  # the display name
    steps_can: tuple[str, ...] = ()  # the server's own tool names, unmounted here, a step's to call
    turned_off: tuple[str, ...] = ()  # off in the owner's connector settings: no step calls them


class ProbeResult(BaseModel):
    """What one initialize + list_tools round trip learned - already sanitized and capped."""

    model_config = ConfigDict(frozen=True)

    tools: tuple[ConnectorToolInfo, ...] = ()
    instructions: str = ""
    server_info: dict[str, str] = Field(default_factory=dict)


# ── slug ──────────────────────────────────────────────────────────────────────────────────────

_SLUG_MAX: Final = 32


def slugify(name: str) -> str:
    """A connector's slug prefixes every tool name, so it must be identifier-safe: lowercase
    `[a-z0-9_]`, starting with a letter, ≤32 chars. Deterministic - the same name always slugs
    the same way, which is what makes the prefix stable across park/resume."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:_SLUG_MAX].rstrip("_")
    if not slug or not slug[0].isalpha():
        slug = f"c_{slug}"[:_SLUG_MAX].rstrip("_")
    return slug


# ── PKCE (RFC 7636, S256 only) ────────────────────────────────────────────────────────────────


def s256_challenge(verifier: str) -> str:
    """base64url(sha256(verifier)), unpadded - the S256 method, and the only one we ever send."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── the phone apps' return (RFC 8252) ───────────────────────────────────────────────────────

# One private-use scheme per app build. A fixed allow-list: the place a callback sends the
# browser is never taken from a request, only picked from these by name.
APP_RETURN_SCHEMES: Final = ("prismoid", "prismoid-dev", "prismoid-staging")


def app_return_url(scheme: str, connector_id: str, *, return_code: str | None) -> str:
    """Where an app sign-in's callback sends the browser: the app, with the connector and either
    the one-time return code (`ok=1`) or nothing to finish (`ok=0`, the row says why). Only
    server-minted values ride it - never provider text."""
    if scheme not in APP_RETURN_SCHEMES:
        raise ValueError(f"not an app return scheme: {scheme!r}")
    query: dict[str, str] = {"connector_id": connector_id, "ok": "1" if return_code else "0"}
    if return_code:
        query["return_code"] = return_code
    return f"{scheme}://connectors/return?{urlencode(query)}"


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge). 64 random bytes → 86 urlsafe chars, inside RFC 7636's 43–128."""
    verifier = secrets.token_urlsafe(64)
    return verifier, s256_challenge(verifier)


# ── discovery URL derivation (RFC 9728 · RFC 8414 · OIDC · the MCP-spec fallback) ────────────


def _split(url: str) -> tuple[str, str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc, parts.path.rstrip("/")


def protected_resource_urls(server_url: str) -> list[str]:
    """RFC 9728 candidates for a server URL, most specific first: the path-inserted well-known,
    then the root one. `https://host/mcp` → `…/.well-known/oauth-protected-resource/mcp`,
    `…/.well-known/oauth-protected-resource`."""
    scheme, host, path = _split(server_url)
    urls = [f"{scheme}://{host}/.well-known/oauth-protected-resource{path}"]
    if path:
        urls.append(f"{scheme}://{host}/.well-known/oauth-protected-resource")
    return urls


def auth_server_metadata_urls(issuer: str) -> list[str]:
    """RFC 8414 candidates for an issuer, in the order the MCP spec walks them: OAuth metadata
    with path insertion, then root; then OIDC discovery with path insertion, then the OIDC
    path-*appended* legacy shape."""
    scheme, host, path = _split(issuer)
    urls = [f"{scheme}://{host}/.well-known/oauth-authorization-server{path}"]
    if path:
        urls.append(f"{scheme}://{host}/.well-known/oauth-authorization-server")
    urls.append(f"{scheme}://{host}/.well-known/openid-configuration{path}")
    if path:
        urls.append(f"{scheme}://{host}{path}/.well-known/openid-configuration")
    return urls


def default_endpoints(auth_base: str) -> dict[str, str]:
    """The MCP spec's last resort for servers publishing no metadata at all: fixed paths on the
    authorization base URL."""
    scheme, host, _ = _split(auth_base)
    root = f"{scheme}://{host}"
    return {
        "authorization_endpoint": f"{root}/authorize",
        "token_endpoint": f"{root}/token",
        "registration_endpoint": f"{root}/register",
    }


_RESOURCE_METADATA_RE: Final = re.compile(r'resource_metadata="([^"]+)"')


def resource_metadata_from_www_auth(header: str) -> str | None:
    """The RFC 9728 hint: `WWW-Authenticate: Bearer resource_metadata=<quoted url>`."""
    match = _RESOURCE_METADATA_RE.search(header or "")
    return match.group(1) if match else None


# ── the two authorize/token legs, assembled (never sent) ─────────────────────────────────────


def authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    resource: str | None = None,
    scopes: str | None = None,
) -> str:
    """The browser leg. `resource` is RFC 8707 - sent whenever the resource metadata declared
    itself; `scope` only when we have one to ask for (an empty scope is a server's decision)."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = scopes
    if resource:
        params["resource"] = resource
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


def _client_auth(
    data: dict[str, str], *, client_id: str, client_secret: str | None, auth_method: str
) -> dict[str, str]:
    """RFC 6749: `client_secret_basic` rides the Authorization header; `client_secret_post`
    rides the form; a public client (`none`) sends only its id. Returns the extra headers."""
    if auth_method == "client_secret_basic" and client_secret is not None:
        raw = f"{client_id}:{client_secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}
    data["client_id"] = client_id
    if auth_method == "client_secret_post" and client_secret is not None:
        data["client_secret"] = client_secret
    return {}


def token_request(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None,
    auth_method: str = "client_secret_post",
    resource: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """The code-exchange leg as (form data, extra headers) - assembled here, POSTed by operations."""
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if resource:
        data["resource"] = resource
    headers = _client_auth(data, client_id=client_id, client_secret=client_secret, auth_method=auth_method)
    return data, headers


def refresh_request(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str | None,
    auth_method: str = "client_secret_post",
    resource: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """The refresh leg, same shape. OAuth 2.1 servers may rotate the refresh token in the answer -
    the caller persists whatever comes back, never assumes it kept the one it sent."""
    data: dict[str, str] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if resource:
        data["resource"] = resource
    headers = _client_auth(data, client_id=client_id, client_secret=client_secret, auth_method=auth_method)
    return data, headers


# ── the door check (SSRF) ─────────────────────────────────────────────────────────────────────
#
# Moved to `schemas/egress.py`: the fetch tool needed the same literal
# refusals, and two copies of an SSRF rule drift into a hole. Re-exported from the top import
# block so this module's public API - what operations and the tests import - is unchanged.

# ── untrusted server text ─────────────────────────────────────────────────────────────────────

INSTRUCTIONS_CAP: Final = 2000
DESCRIPTION_CAP: Final = 300
# Roomier than the card's `DESCRIPTION_CAP`: real tools explain themselves, a hostile one must end.
MOUNT_DESCRIPTION_CAP: Final = 2000


def sanitize_server_text(text: str, cap: int) -> str:
    """Server prose the model reads and the owner approves: only what a person can see, then the cap."""
    return visible(text or "")[:cap].strip()


# ── per-tool policy ───────────────────────────────────────────────────────────────────────────


def default_tool_policy(info: ConnectorToolInfo) -> ToolPolicy:
    """The dial, in ONE place - and it reads ALLOW (reversing the earlier
    ask-default: an unannotated server made every read a dialog, and "tools can automatically be
    picked up" won). CONNECTING is the consent: the user clicked Connect for this service,
    on this account - a second dialog per tool re-asks a question they already answered. The
    machinery all stands: the permissions
    screen sets any tool to `ask` or `off` with one PATCH, the approval gate fires wherever
    `ask` is set, and this function is the one line to change if the posture moves again."""
    return ToolPolicy.ALLOW


def effective_policies(
    overrides: dict[str, str], tools: tuple[ConnectorToolInfo, ...] | list[ConnectorToolInfo]
) -> dict[str, ToolPolicy]:
    """Override wins, else the derived default - computed against the CURRENT inventory, so a
    tool the server grew since the owner last looked gets the safe default, and an override
    for a tool the server dropped simply stops mattering (no garbage collection)."""
    out: dict[str, ToolPolicy] = {}
    for info in tools:
        raw = overrides.get(info.name, "")
        try:
            out[info.name] = ToolPolicy(raw) if raw else default_tool_policy(info)
        except ValueError:
            out[info.name] = default_tool_policy(info)
    return out


# Long enough to show what will actually run: an approval accepted from a prefix runs the whole
# call, so a value or the whole preview past its cap says how much it left out.
PREVIEW_VALUE_CAP: Final = 2000
PREVIEW_TOTAL_CAP: Final = 8000


_NAME_KEYS: Final = ("name", "title", "subject")
# The argument keys a card may name: `id`, `*_id`, `*Id` and their plurals; never an amount or a count.
_ID_KEY: Final = re.compile(r"(?:^|_)ids?$|[a-z0-9]Ids?$")


def named_ids(data: object, into: dict[str, str] | None = None) -> dict[str, str]:
    """Every id in a service's own answer (`data`, parsed JSON) that sits beside a name: one object's
    `id` (or its only `*_id` key) and its `name`, `title` or `subject`, so a card can name an item. An id
    two items share under different names (services number each kind from 1) maps to "", named neither."""
    found = {} if into is None else into
    if isinstance(data, list):
        for item in cast("list[object]", data):
            named_ids(item, found)
    elif isinstance(data, dict):
        node = cast("dict[str, object]", data)
        name = next((v for k in _NAME_KEYS if isinstance(v := node.get(k), str) and v.strip()), None)
        ids = ["id"] if "id" in node else [k for k in node if k.endswith("_id")]
        if name is not None and len(ids) == 1 and isinstance(node[ids[0]], str | int):
            key = str(node[ids[0]])
            found[key] = name if found.get(key, name) == name else ""
        for value in node.values():
            named_ids(value, found)
    return found


def _named(value: object, names: Mapping[str, str]) -> str:
    """`value` as text, followed by the names its ids carry in `names`, when any do."""
    shown = str(value)
    scalars = cast("list[object]", value) if isinstance(value, list) else [value]
    known = [name for v in scalars if (name := names.get(str(v)))]
    return f"{shown} ({'; '.join(known)})" if known else shown


def args_preview(
    args: Mapping[str, object],
    *,
    names: Mapping[str, str] | None = None,
    per_value: int = PREVIEW_VALUE_CAP,
    total: int = PREVIEW_TOTAL_CAP,
) -> str:
    """What the owner sees on an approve gate: sorted `k: v` lines, each value flattened and
    capped, the whole preview capped with an honest marker; an id the run has seen named
    (`names`, from `named_ids`) is followed by that name. This is USER-FACING gate content
    for the person deciding - the flight recorder keeps its arg-keys-only law; the
    consent screen and the journal answer different questions."""
    lines: list[str] = []
    for key in sorted(args):
        # The KEY is model-authored too: flattened exactly like the value, or a key carrying
        # "\n\n" forges an extra evidence block in the card the dock splits on blank lines.
        shown_key = visible(str(key).replace("\n", " "))[:per_value]
        named = (names or {}) if _ID_KEY.search(str(key)) else {}
        value = visible(_named(args[key], named).replace("\n", " "))
        if len(value) > per_value:
            value = f"{value[:per_value]}… ({len(value) - per_value} more characters not shown)"
        lines.append(f"{shown_key}: {value}")
    preview = "\n".join(lines)
    if len(preview) > total:
        preview = f"{preview[:total].rstrip()}\n… ({len(preview) - total} more characters not shown)"
    return preview
