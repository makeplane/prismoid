# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tables of the isolation suite (ADR-061), shared by the `test_workspace_isolation*`
files: what each route needs to be called with, which routes are exempt and why, the cross-references
and the positive lists. A plain module, not a test file."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import BaseRoute

from prismoid.api.app import create_app

PASSWORD = "a-long-enough-password"


TIMEOUT_S = 10  # a stream that answers instead of refusing must fail, never hang the suite


BOT_MD = """---
name: {name}
description: >
  Writes release notes from shipped work. Load when: a release needs notes. Not for: contracts.
capability: fast-executor
produces: document
---
Write plainly.
"""


SKILL_MD = """---
description: Load when the user wants an isolation memo. Not for spreadsheets or code.
capability: fast-executor
---
Write the isolation memo.
"""


SKILL_CASES = "routing:\n  - goal: write an isolation memo\n    expect: {name}\n"


SCHEDULE = {"cron": "0 9 * * 1-5", "timezone": "UTC"}


EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/v1/auth/signup"): "no tenant data: creates a person and their own personal workspace",
    ("POST", "/v1/auth/login"): "no tenant data: credentials in, the caller's own session out",
    (
        "POST",
        "/v1/auth/forgot-password",
    ): "person-level, platform mode: an emailed cr_ code, no workspace row",
    ("POST", "/v1/auth/reset-password"): "person-level, platform mode: the cr_ code is the auth",
    (
        "POST",
        "/v1/auth/logout",
    ): "revokes the caller's own credential only; names no row (and would end the walk)",
    ("GET", "/v1/templates/bots/{slug}"): (
        "the platform share catalog by design (ADR-056 §4): an anonymous preview of a PUBLISHED head"
    ),
    ("POST", "/v1/templates/bots/{slug}/install"): (
        "installing a shared template across workspaces IS the feature (ADR-056 §4); the install lands in "
        "the installer's workspace - test_workspace_fetchers::"
        "test_a_template_install_lands_in_the_bound_workspace_and_wires_only_its_connectors"
    ),
    ("POST", "/v1/channels/inbound/email"): (
        "operator webhook with no caller identity (auth exception #7): the shared header secret is "
        "the whole auth and the recipient address binds the CHANNEL's workspace - covered by "
        "test_workspace_door::test_the_email_inbound_webhook_lands_in_the_channels_workspace (lands in the "
        "channel's workspace; without the secret a real address answers exactly like an absent one)"
    ),
    ("GET", "/v1/blobs/{digest}"): (
        "local mode's download door (ADR-077): no Actor and no workspace to tell apart, the HMAC `sig` "
        "is the whole auth, and it 404s for everyone off local + fs (test_blob_door)"
    ),
    ("GET", "/watch/{task_id}"): (
        "inert HTML with no data; its token authenticates the EventSource on "
        "/v1/tasks/{task_id}/events, which is walked here"
    ),
    ("GET", "/healthz"): "no tenant data: the load balancer's bare status",
}


# /v1/admin/* is exempt as a family: the operator console runs in platform mode, cross-workspace by
# design - and every one of those routes refuses a non-admin, which
# test_admin_surface::test_every_admin_route_holds_the_gate proves from the same router.
_ADMIN_PREFIX = "/v1/admin/"


# Writes without path parameters that carry no reference to another row: they create or edit in
# the caller's OWN workspace or profile, so there is no A id to hand them.
OWN_ONLY: dict[tuple[str, str], str] = {
    ("PATCH", "/v1/auth/me"): "edits the caller's own profile",
    ("POST", "/v1/auth/api-keys"): "mints a key for the caller in the caller's bound workspace",
    ("POST", "/v1/projects"): "creates a project in the caller's workspace; the body names no other row",
    ("POST", "/v1/connectors"): "creates a connector from a server URL; names no other row",
    ("POST", "/v1/connectors/connect"): "catalog slug or server URL; names no other row",
    ("POST", "/v1/egress-allowances"): "a host name for the caller's own allowlist",
    ("POST", "/v1/skills"): "a skill name is per-workspace (ADR-061): B's push lands in B's workspace",
    ("POST", "/v1/workspaces"): "founds a new team workspace owned by the caller; the body is only a name",
    ("POST", "/v1/workspaces/invites/accept"): (
        "the oi_ code is a bearer secret and the email match is the check - a stranger holding "
        "A's code but not A's invitee address is refused 403 and the invite stays pending "
        "(test_workspaces_api::test_another_logged_in_person_cannot_spend_the_invite)"
    ),
    ("POST", "/v1/projects/invites/accept"): (
        "the si_ code is a bearer secret whose row binds its own workspace; a stranger outside that workspace "
        "gets the same 404 as an unknown code "
        "(test_project_invites::test_every_dead_code_answers_the_same_404)"
    ),
    ("POST", "/v1/auth/me/verify-email"): (
        "proves the caller's own address; the code is matched to the caller, so one minted for "
        "anyone else is the same 422 as a dead one"
    ),
    (
        "POST",
        "/v1/auth/me/verification",
    ): "mails a fresh code to the caller's own address; names no other row",
    ("POST", "/v1/inbox/read-all"): (
        "marks the caller's own inbox read; probed below, and A's item must still be unread after"
    ),
    ("POST", "/v1/push/devices"): (
        "registers the caller's own phone for push (ADR-100); the body names no row, and its FCM token is "
        "a secret only the phone and the server hold (no route returns one)"
    ),
}


# Where a refusal other than 404 is the honest answer. Always ALSO equal to the answer for an id
# that names nothing - an override never excuses an existence oracle.
REFUSAL: dict[tuple[str, str], set[int]] = {
    # The bc_ channel token is these routes' whole auth; any other credential is one
    # indistinguishable 401 (the fire door's law) - "wrong, revoked and never-minted" alike.
    ("POST", "/v1/channels/{channel_id}/messages"): {401},
    ("GET", "/v1/channels/{channel_id}/messages"): {401},
    # A name is a FILTER over the caller's own rows here, not a lookup: a name nobody has in
    # B's workspace answers an empty list, exactly as A's name does.
    ("GET", "/v1/bots/{name}/runs"): {200},
    ("GET", "/v1/bots/{name}/artifacts"): {200},
    ("GET", "/v1/bots/{name}/channels"): {200},
    ("GET", "/v1/bots/{name}/routines"): {200},
    ("GET", "/v1/bots/{name}/stats"): {200},
    # A push is an upsert keyed by (workspace, name): B's push of A's name mints B's OWN draft in B's
    # workspace, exactly as for a name nobody has - and A's row is checked untouched after the walk.
    ("PUT", "/v1/bots/{name}"): {200, 201},
    ("PUT", "/v1/skills/{name}"): {200, 201},
}


# Path parameter → the key of the seeded value (`name` is two resources' key).
PARAM_KEYS = frozenset(
    {
        "task_id",
        "project_id",
        "actor_id",
        "capture_id",
        "item_id",
        "gate_id",
        "artifact_id",
        "memory_id",
        "connector_id",
        "allowance_id",
        "grant_id",
        "key_id",
        "skill_name",
        "bot_name",
        "slug",
        "conversation_id",
        "seq",
        "channel_id",
        "routine_id",
        "workspace_id",
        "invite_id",
        "project_invite_id",
        "command_id",
        "device_id",
    }
)


# Query parameter → seeded key: the list routes are walked again with A's values in them.
QUERY_REFS = {"project_id": "project_id", "bot": "bot_name", "skill": "skill_name", "q": "task_title"}


WS_ROUTES = frozenset({"/v1/tasks/{task_id}/stream", "/v1/tasks/{task_id}/screen"})


def key_for(path: str, param: str) -> str:
    if param == "name":
        return "skill_name" if path.startswith("/v1/skills") else "bot_name"
    return param


def api_routes(routes: Iterable[BaseRoute]) -> list[BaseRoute]:
    """Every APIRoute and WebSocket route, nesting included - FastAPI 0.139 mounts an included
    router as one `_IncludedRouter` node reachable only through `.original_router` (the walk in
    test_admin_surface explains the trap: a flat comprehension found four routes)."""
    found: list[BaseRoute] = []
    for route in routes:
        if isinstance(route, APIRoute | APIWebSocketRoute):
            found.append(route)
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            nested = getattr(child, "routes", child)
            if nested:
                found.extend(api_routes(nested))
    return found


def endpoints() -> list[tuple[str, str, APIRoute]]:
    app = create_app()
    out = [
        (method, route.path, route)
        for route in api_routes(app.routes)
        if isinstance(route, APIRoute) and not route.path.startswith(_ADMIN_PREFIX)
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
    ]
    assert len(out) > 120, "the walk must find the product surface, not an empty list"
    return out


def iso_params(route: APIRoute) -> list[str]:
    return list(route.param_convertors.keys())


Vals = dict[str, str]


BodyFn = Callable[[Vals], Any]


BODIES: dict[tuple[str, str], BodyFn] = {
    ("PATCH", "/v1/tasks/{task_id}"): lambda v: {"title": "b was here"},
    ("POST", "/v1/tasks/{task_id}/move"): lambda v: {"project_id": None},
    ("PUT", "/v1/tasks/{task_id}/budget"): lambda v: {"usd": "10"},
    ("POST", "/v1/tasks/{task_id}/steer"): lambda v: {"directive": "b steers"},
    ("POST", "/v1/tasks/{task_id}/replan"): lambda v: {"directive": "b replans"},
    ("POST", "/v1/tasks/{task_id}/follow_up"): lambda v: {"goal": "b follows up"},
    ("POST", "/v1/tasks/{task_id}/retry"): lambda v: {},
    ("POST", "/v1/tasks/{task_id}/feedback"): lambda v: {"rating": "down"},
    ("POST", "/v1/tasks/{task_id}/fork"): lambda v: {},
    ("POST", "/v1/tasks/{task_id}/presence"): lambda v: {
        "conn": "isolation1",
        "typing": True,
        "mode": "steer",
    },
    ("POST", "/v1/tasks/{task_id}/screen/control"): lambda v: {"conn": "isolation1", "private": False},
    ("POST", "/v1/tasks/{task_id}/screen/ticket"): lambda v: {"conn": "isolation1"},
    ("PATCH", "/v1/workspaces/{workspace_id}"): lambda v: {"name": "b renamed it"},
    ("PATCH", "/v1/workspaces/{workspace_id}/members/{actor_id}"): lambda v: {"role": "guest"},
    ("POST", "/v1/workspaces/{workspace_id}/transfer"): lambda v: {"actor_id": v["a_id"]},
    ("POST", "/v1/workspaces/{workspace_id}/invites"): lambda v: {
        "email": "b-invites@example.com",
        "role": "member",
    },
    ("PATCH", "/v1/projects/{project_id}"): lambda v: {"name": "b renamed it"},
    ("POST", "/v1/projects/{project_id}/members"): lambda v: {"email": v["b_email"], "role": "editor"},
    ("PATCH", "/v1/projects/{project_id}/members/{actor_id}"): lambda v: {"role": "viewer"},
    ("POST", "/v1/projects/{project_id}/transfer"): lambda v: {"email": v["b_email"]},
    ("POST", "/v1/projects/{project_id}/invites"): lambda v: {"role": "viewer"},
    ("POST", "/v1/capture/{capture_id}/promote"): lambda v: {"goal": ""},
    ("POST", "/v1/gates/{gate_id}/resolve"): lambda v: {"action": "accept"},
    ("POST", "/v1/artifacts/{artifact_id}/revise"): lambda v: {
        "content": "b rewrote it",
        "base_sha256": v["artifact_sha"],
    },
    ("POST", "/v1/artifacts/{artifact_id}/restore"): lambda v: {"base_sha256": v["artifact_sha"]},
    ("POST", "/v1/artifacts/{artifact_id}/label"): lambda v: {"label": "b"},
    ("PATCH", "/v1/memories/{memory_id}"): lambda v: {"text": "b rewrote it"},
    ("PATCH", "/v1/connectors/{connector_id}"): lambda v: {"name": "b"},
    ("PATCH", "/v1/connectors/{connector_id}/policies"): lambda v: {"policies": {}},
    ("POST", "/v1/connectors/{connector_id}/authorize"): lambda v: {},
    ("POST", "/v1/connectors/{connector_id}/oauth/finish"): lambda v: {
        "return_code": "ca_" + "b" * 43,
        "code_verifier": "b" * 43,
    },
    ("PUT", "/v1/skills/{name}"): lambda v: {
        "skill_md": SKILL_MD,
        "cases_yaml": SKILL_CASES.format(name=v["skill_name"]),
    },
    ("PUT", "/v1/bots/{name}"): lambda v: {"bot_md": BOT_MD.format(name=v["bot_name"])},
    ("PATCH", "/v1/bots/{name}"): lambda v: {"title": "b"},
    ("PUT", "/v1/bots/{name}/pin"): lambda v: {"pinned": True},
    ("POST", "/v1/bots/{name}/channels"): lambda v: {"kind": "webhook", "label": "b"},
    ("PATCH", "/v1/conversations/{conversation_id}"): lambda v: {"title": "b"},
    ("POST", "/v1/conversations/{conversation_id}/messages"): lambda v: {"text": "b says hi"},
    ("POST", "/v1/conversations/{conversation_id}/messages/{seq}/save"): lambda v: {"format": "md"},
    ("PATCH", "/v1/channels/{channel_id}"): lambda v: {"daily_message_cap": 5},
    ("POST", "/v1/channels/{channel_id}/messages"): lambda v: {"message": "b says hi"},
    ("PATCH", "/v1/routines/{routine_id}"): lambda v: {"name": "b"},
    ("POST", "/v1/routines/{routine_id}/pause"): lambda v: {},
}


def task_body(**extra: Any) -> dict[str, Any]:
    return {"goal": "b's own work", "clarify": "never", "budget": {"usd": "1"}, **extra}


def _routine_body(**extra: Any) -> dict[str, Any]:
    body = {"name": "b-routine", "instruction": "b's standing work", "schedule": SCHEDULE}
    return {**body, "budget_usd": 1, **extra}


# Writes without path parameters whose body REFERENCES one of A's rows: each must answer as it
# does for a reference to nothing, and refuse. (label, method, path, body)
CROSS_REFS: list[tuple[str, str, str, BodyFn]] = [
    ("task into A's project", "POST", "/v1/tasks", lambda v: task_body(project_id=v["project_id"])),
    (
        "task mounting A's connector",
        "POST",
        "/v1/tasks",
        lambda v: task_body(connectors=[v["connector_id"]]),
    ),
    ("task rostering A's bot", "POST", "/v1/tasks", lambda v: task_body(bots=[v["bot_name"]])),
    ("task pinning A's skill", "POST", "/v1/tasks", lambda v: task_body(skills=[v["skill_name"]])),
    (
        "device grant on A's task",
        "POST",
        "/v1/device/grants",
        lambda v: {"task_id": v["task_id"], "kind": "docker"},
    ),
    (
        "capture into A's project",
        "POST",
        "/v1/capture",
        lambda v: {"text": "b", "project_id": v["project_id"]},
    ),
    (
        "memory in A's project",
        "POST",
        "/v1/memories",
        lambda v: {"scope": "project", "project_id": v["project_id"], "text": "b"},
    ),
    (
        "bot in A's project",
        "POST",
        "/v1/bots",
        lambda v: {"name": "b-bot", "bot_md": BOT_MD.format(name="b-bot"), "project_id": v["project_id"]},
    ),
    (
        "template of A's bot",
        "POST",
        "/v1/templates/bots",
        lambda v: {"name": v["bot_name"], "author_name": "B"},
    ),
    ("conversation with A's bot", "POST", "/v1/conversations", lambda v: {"bot": v["bot_name"]}),
    (
        "conversation in A's project",
        "POST",
        "/v1/conversations",
        lambda v: {"bot": "one", "project_id": v["project_id"]},
    ),
    ("routine into A's project", "POST", "/v1/routines", lambda v: _routine_body(project_id=v["project_id"])),
    (
        "session into A's workspace",
        "PUT",
        "/v1/auth/session/workspace",
        lambda v: {"workspace_id": v["workspace_id"]},
    ),
]


# What a viewer may legitimately see or do with A's workspace because it is THEIR person-level fact,
# not workspace A's data: A herself lists her memberships (the switcher) and may switch back; and a
# bot template is a PLATFORM row whose author is a person ("bot templates stay a
# cross-workspace share link by design") - its author lists and unlists it from whichever workspace she is
# in. The stranger walk holds both routes to the full law.
_TEMPLATE_BY_PERSON = "a bot template is a platform row authored by a person (ADR-061 registries)"


PERSON_LEVEL: dict[str, dict[tuple[str, str], set[str]]] = {
    "self_elsewhere": {
        ("GET", "/v1/workspaces"): {"workspace_id"},
        ("GET", "/v1/templates/bots"): {"slug", "bot_name"},
    },
}


_MEMBERSHIP_IS_THE_AUTH = (
    "a workspace-management route: the caller's MEMBERSHIP in the workspace the path names is the authorization "
    "(ADR-061) - a person leaves or manages any workspace they belong to from whichever they are in. The "
    "stranger walk holds it to the full 404 law; the role gates are test_workspaces_api's"
)


_WORKSPACE_ROUTES = (
    ("GET", "/v1/workspaces/{workspace_id}"),
    ("PATCH", "/v1/workspaces/{workspace_id}"),
    ("GET", "/v1/workspaces/{workspace_id}/members"),
    ("POST", "/v1/workspaces/{workspace_id}/members/me/leave"),
    ("PATCH", "/v1/workspaces/{workspace_id}/members/{actor_id}"),
    ("DELETE", "/v1/workspaces/{workspace_id}/members/{actor_id}"),
    ("POST", "/v1/workspaces/{workspace_id}/transfer"),
    ("GET", "/v1/workspaces/{workspace_id}/invites"),
    ("POST", "/v1/workspaces/{workspace_id}/invites"),
    ("DELETE", "/v1/workspaces/{workspace_id}/invites/{invite_id}"),
)


_PHONE_IS_THE_PERSONS = (
    "a phone is the person's, not a workspace's (ADR-100): A removes her own from whichever workspace she "
    "is in. The stranger walk holds it to the full 404 law"
)


PERSON_LEVEL_SKIP: dict[str, dict[tuple[str, str], str]] = {
    "self_elsewhere": {
        ("POST", "/v1/templates/bots/{slug}/unpublish"): _TEMPLATE_BY_PERSON,
        ("DELETE", "/v1/push/devices/{device_id}"): _PHONE_IS_THE_PERSONS,
        **dict.fromkeys(_WORKSPACE_ROUTES, _MEMBERSHIP_IS_THE_AUTH),
    },
}


SKIP_CROSS_REF: dict[str, set[str]] = {
    # a member switching into her own workspace is the feature (test_workspace_door::
    # test_switch_moves_the_session_and_an_api_key_cannot_switch); a stranger is refused above
    "self_elsewhere": {"session into A's workspace"},
}


# Writes on B's OWN row whose body references A's: the same law. (label, method, path, body)
CROSS_REFS_ON_OWN: list[tuple[str, str, str, BodyFn]] = [
    (
        "B's task moved into A's project",
        "POST",
        "/v1/tasks/{own_task_id}/move",
        lambda v: {"project_id": v["project_id"]},
    ),
]


CROSS_REF_ROUTES = {(m, p) for _, m, p, _ in CROSS_REFS}


# Lists where A's own view must show her row - the positive control that B's absence means
# something. route → the seeded key whose value must appear.
POSITIVE_LISTS: dict[str, str] = {
    "/v1/tasks": "task_id",
    "/v1/projects": "project_id",
    "/v1/capture": "capture_id",
    "/v1/inbox": "item_id",
    "/v1/gates": "gate_id",
    "/v1/memories": "memory_id",
    "/v1/connectors": "connector_id",
    "/v1/egress-allowances": "allowance_id",
    "/v1/device/grants": "grant_id",
    "/v1/skills": "skill_name",
    "/v1/bots": "bot_name",
    "/v1/conversations?bot={bot_name}": "conversation_id",
    "/v1/routines": "routine_id",
    "/v1/auth/api-keys": "key_id",
    "/v1/plugins": "plugin_name",
    "/v1/templates/bots": "slug",
    "/v1/templates/bots/installed": "slug",
    "/v1/workspaces": "workspace_id",
}
