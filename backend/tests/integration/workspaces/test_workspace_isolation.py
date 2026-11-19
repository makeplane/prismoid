# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The isolation suite - Phase 0b's exit criterion as a test (ADR-061).

"Two workspaces on one deployment cannot see each other." Workspace A is seeded with one of every
tenant root and child (project, task + its events, gate, inbox item, artifact, conversation +
message, bot, channel, routine, connector, egress allowance, capture, memory, published skill,
API key, plugin install, template install). Person B signs up into a workspace of their own - never
added to A's - and walks EVERY route the app serves, read from the router, not listed here:

- a route with path parameters is called with A's ids AND with ids that name nothing; the two
  answers must be indistinguishable (same status, same body once the ids are masked) and a
  refusal (404 unless a named override says why another status is the honest one). An answer
  that differs from "absent" is an existence oracle - a leak even when it is a 403.
- a route without path parameters that reads (GET) must contain none of A's markers - bare,
  and again with A's ids/names in every query parameter that takes one.
- a route without path parameters that writes is either probed with a body that REFERENCES
  one of A's rows (answered as for a reference to nothing, and refused), or named in OWN_ONLY
  with the reason it cannot reach another workspace's row.
- the WebSocket routes are probed at the ASGI level: the task stream must close 4404 without
  accepting; the screen accepts first (a close code before accept is an HTTP 403 to a browser) and
  must then say only `bye{unauthorized}` and close 4401 - with no ticket, and with a ticket the
  viewer minted for its OWN task presented on this path (a ticket names its task).

Positive controls keep the proof honest: A gets 200 on the same detail routes (so every seeded id
is real) and finds her rows on the same lists, and after B's walk A's rows are untouched.

A route the suite cannot exercise must be in EXEMPT with its reason, so a new route cannot slip
past: `test_every_route_is_covered_or_exempt` fails for any route it does not know how to walk.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select

from prismoid import storage
from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot_template import BotTemplate
from prismoid.db.models.bot_template_install import BotTemplateInstall
from prismoid.db.models.connector import Connector
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.inbox import InboxItem
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.plugin_install import PluginInstall
from prismoid.db.models.push_device import PushDevice
from prismoid.db.models.task import Task
from prismoid.db.models.verification import Verification
from prismoid.db.models.workspace import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import artifacts as artifact_ops
from prismoid.operations import auth as auth_ops
from prismoid.operations import bots as bot_ops
from prismoid.operations import gates as gate_ops
from prismoid.operations import skills as skill_ops
from prismoid.operations import steering, tickets
from prismoid.schemas.artifacts import Manifest
from prismoid.schemas.bots import SelectionCaseResult, SelectionReport
from prismoid.schemas.ids import StepId
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.screen import decode
from prismoid.schemas.verification import Verdict, VerificationReport
from tests.support.cast import verify_email
from tests.support.rigs.workspace_isolation_rig import (
    BODIES,
    BOT_MD,
    CROSS_REF_ROUTES,
    CROSS_REFS,
    CROSS_REFS_ON_OWN,
    EXEMPT,
    OWN_ONLY,
    PARAM_KEYS,
    PASSWORD,
    PERSON_LEVEL,
    PERSON_LEVEL_SKIP,
    POSITIVE_LISTS,
    QUERY_REFS,
    REFUSAL,
    SCHEDULE,
    SKILL_CASES,
    SKILL_MD,
    SKIP_CROSS_REF,
    TIMEOUT_S,
    BodyFn,
    Vals,
    endpoints,
    iso_params,
    key_for,
    task_body,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("push_on")]  # A registers a phone


@dataclass
class Viewer:
    """Who walks the router from outside workspace A, bound to a workspace that is not A's."""

    headers: dict[str, str]
    token: str
    own_task_id: str  # a task of the viewer's own, in the viewer's workspace
    sees: set[str] = field(default_factory=set)  # A's markers that are the viewer's own identity


@dataclass
class Seeded:
    a_headers: dict[str, str]
    ids: dict[str, str]  # by _PARAM_KEYS, plus the extras the bodies and checks read
    viewers: dict[str, Viewer]
    a_markers: set[str] = field(default_factory=set)


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _ok(r: Response, *codes: int) -> Any:
    assert r.status_code in (codes or (200, 201, 202)), f"{r.request.method} {r.request.url}: {r.text}"
    return r.json()


async def _signup(client: AsyncClient, name: str, email: str) -> str:
    r = await client.post("/v1/auth/signup", json={"email": email, "name": name, "password": PASSWORD})
    token = str((await _ok(r, 201))["token"])
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    await verify_email((await _ok(me, 200))["id"])  # an inviter's address is proven (ADR-086)
    return token


@pytest.fixture(autouse=True)
async def quiet_hub() -> AsyncIterator[None]:
    yield
    await hub.stop()


@pytest.fixture
async def client(silent_dispatcher: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    await auth_ops.flush_outbound_email()


@pytest.fixture
async def seeded(client: AsyncClient) -> AsyncIterator[Seeded]:
    """Workspace A through the real API wherever the API can make the row; bound inserts for the rows
    only a run makes (gate, inbox knock, artifact) or that need a remote server (connector)."""
    tag = uuid.uuid4().hex[:8]
    a_email, b_email = f"acme{tag}@example.com", f"bravo{tag}@example.com"
    a_token = await _signup(client, f"Acme Ada {tag}", a_email)
    b_token = await _signup(client, f"Bravo Bob {tag}", b_email)
    a, b = _h(a_token), _h(b_token)
    a_me = await _ok(await client.get("/v1/auth/me", headers=a))
    b_me = await _ok(await client.get("/v1/auth/me", headers=b))
    assert a_me["workspace"]["id"] != b_me["workspace"]["id"], "two people, two workspaces"

    ids: dict[str, str] = {
        "a_id": a_me["id"],
        "workspace_id": a_me["workspace"]["id"],
        "a_email": a_email,
        "b_email": b_email,
        "task_title": f"acme-task-{tag}",
    }
    # A's phone (ADR-100): a person's row, registered with A's login session.
    phone = {"token": f"acme-fcm-{tag}", "platform": "ios", "app_id": "so.acme.app", "app_version": "1.0"}
    ids["device_id"] = (await _ok(await client.post("/v1/push/devices", headers=a, json=phone), 201))["id"]
    invited = await client.post(
        f"/v1/workspaces/{ids['workspace_id']}/invites",
        headers=a,
        json={"email": f"invitee{tag}@example.com"},
    )
    ids["invite_id"] = (await _ok(invited, 201))["id"]
    ids["project_id"] = (
        await _ok(await client.post("/v1/projects", headers=a, json={"name": f"acme-project-{tag}"}))
    )["id"]
    si = await client.post(f"/v1/projects/{ids['project_id']}/invites", headers=a, json={"role": "viewer"})
    ids["project_invite_id"] = (await _ok(si, 201))["id"]
    # A teammate in A's workspace, sharing A's project - the member the /members/{actor_id} routes name.
    carol_email = f"carol{tag}@example.com"
    carol = _h(await _signup(client, f"Acme Carol {tag}", carol_email))
    ids["actor_id"] = (await _ok(await client.get("/v1/auth/me", headers=carol)))["id"]
    async with session_scope() as s:
        await WorkspaceMember.add(
            s, uuid.UUID(ids["workspace_id"]), uuid.UUID(ids["actor_id"]), WorkspaceRole.MEMBER
        )
        await s.commit()
    shared = await client.post(
        f"/v1/projects/{ids['project_id']}/members", headers=a, json={"email": carol_email, "role": "editor"}
    )
    await _ok(shared, 201)
    task = await client.post(
        "/v1/tasks",
        headers=a,
        json={
            "goal": f"{ids['task_title']} write a short memo",
            "clarify": "never",
            "budget": {"usd": "1"},
            "project_id": ids["project_id"],
        },
    )
    ids["task_id"] = (await _ok(task))["task_id"]
    bot = f"acme-bot-{tag}"
    cases = f"selection:\n  - goal: Write the release notes\n    expect: {bot}\n"
    made = await client.post(
        "/v1/bots", headers=a, json={"name": bot, "bot_md": BOT_MD.format(name=bot), "cases_yaml": cases}
    )
    await _ok(made, 201)
    ids["bot_name"] = bot
    # PUBLISHED, not a draft: a published head is what projects, rosters and templates share, so it
    # is the version a missed fence would hand across. The green verdict is set, not earned.
    async with session_scope(workspace_id=uuid.UUID(ids["workspace_id"])) as s:
        ada = await s.get(Actor, uuid.UUID(ids["a_id"]))
        assert ada is not None
        case = SelectionCaseResult(goal="Write the release notes", expected=bot, chosen=bot, ok=True)
        report = SelectionReport(bot=bot, version=1, passed=1, total=1, cases=(case,))
        await bot_ops.apply_eval(s, actor=ada, name=bot, version=1, report=report)
        await bot_ops.publish(s, actor=ada, name=bot)
    shared_link = await client.post(
        "/v1/templates/bots", headers=a, json={"name": bot, "author_name": f"Acme Ada {tag}"}
    )
    ids["slug"] = (await _ok(shared_link, 201))["slug"]
    installed = await client.post(
        f"/v1/templates/bots/{ids['slug']}/install", headers=a, json={"rename": f"acme-installed-{tag}"}
    )
    await _ok(installed, 201)
    conv = await client.post("/v1/conversations", headers=a, json={"bot": bot, "title": f"acme-conv-{tag}"})
    ids["conversation_id"] = (await _ok(conv))["id"]
    sent = await client.post(
        f"/v1/conversations/{ids['conversation_id']}/messages", headers=a, json={"text": f"acme-line-{tag}"}
    )
    ids["seq"] = str((await _ok(sent))["seq"])
    channel = await client.post(
        f"/v1/bots/{bot}/channels", headers=a, json={"kind": "webhook", "label": f"acme-channel-{tag}"}
    )
    ids["channel_id"] = (await _ok(channel))["id"]
    routine = await client.post(
        "/v1/routines",
        headers=a,
        json={
            "name": f"acme-routine-{tag}",
            "instruction": "post the acme standup summary",
            "schedule": SCHEDULE,
            "budget_usd": 1,
            "project_id": ids["project_id"],
        },
    )
    ids["routine_id"] = (await _ok(routine))["id"]
    await _ok(await client.post(f"/v1/routines/{ids['routine_id']}/token", headers=a))
    allowance = await client.post(
        "/v1/egress-allowances", headers=a, json={"host": f"acme-{tag}.example.com"}
    )
    ids["allowance_id"] = (await _ok(allowance))["id"]
    # A device grant on A's task, as its row: A's task runs on this worker's
    # cloud driver, and the door grants only to a task on the device - the reads and the revoke
    # it guards do not ask.
    async with session_scope(workspace_id=uuid.UUID(ids["workspace_id"])) as s:
        grant = DeviceGrant(
            task_id=uuid.UUID(ids["task_id"]), kind="docker", created_by_actor_id=uuid.UUID(ids["a_id"])
        )
        s.add(grant)
        await s.commit()
        ids["grant_id"] = str(grant.id)
    capture = await client.post(
        "/v1/capture", headers=a, json={"text": f"acme-capture-{tag}", "project_id": ids["project_id"]}
    )
    ids["capture_id"] = (await _ok(capture))["id"]
    ids["memory_id"] = (
        await _ok(await client.post("/v1/memories", headers=a, json={"text": f"acme-memory-{tag}"}))
    )["id"]
    ids["key_id"] = (
        await _ok(await client.post("/v1/auth/api-keys", headers=a, json={"name": f"acme-key-{tag}"}))
    )["id"]
    skill = f"acme-skill-{tag}"
    pushed = await client.post(
        "/v1/skills",
        headers=a,
        json={"name": skill, "skill_md": SKILL_MD, "cases_yaml": SKILL_CASES.format(name=skill)},
    )
    ids["skill_name"] = skill
    b_task = await client.post("/v1/tasks", headers=b, json=task_body())
    viewers = {"stranger": Viewer(b, b_token, (await _ok(b_task))["task_id"])}
    # A herself, in a second session switched to a second workspace she belongs to: the walk that
    # proves the WORKSPACE fence rather than the person fence - her own rows must not follow her.
    login = await client.post("/v1/auth/login", json={"email": a_email, "password": PASSWORD})
    elsewhere_token = str((await _ok(login))["token"])
    elsewhere = _h(elsewhere_token)
    async with session_scope() as s:
        zeta = await Workspace.create(
            s, name=f"Zeta {tag}", kind=WorkspaceKind.TEAM, created_by=uuid.UUID(ids["a_id"])
        )
        await WorkspaceMember.add(s, zeta.id, uuid.UUID(ids["a_id"]), WorkspaceRole.OWNER)
        await s.commit()
        zeta_id = str(zeta.id)
    switched = await client.put(
        "/v1/auth/session/workspace", headers=elsewhere, json={"workspace_id": zeta_id}
    )
    assert (await _ok(switched))["workspace"]["id"] == zeta_id
    own = await client.post("/v1/tasks", headers=elsewhere, json=task_body())
    viewers["self_elsewhere"] = Viewer(
        elsewhere,
        elsewhere_token,
        (await _ok(own))["task_id"],
        sees={ids["a_id"], a_email, f"Acme Ada {tag}"},  # a person is global
    )

    # The rows only a run (or a remote server) makes - inserted bound to A's workspace, as A.
    workspace_a = uuid.UUID(ids["workspace_id"])
    async with session_scope(workspace_id=workspace_a) as s:
        ada = await s.get(Actor, uuid.UUID(ids["a_id"]))
        task_row = await s.get(Task, uuid.UUID(ids["task_id"]))
        assert ada is not None and task_row is not None
        await skill_ops.apply_eval(s, skill, version=(await _ok(pushed, 201))["version"], green=True)
        await skill_ops.publish(s, actor_id=ada.id, name=skill)

        # A's own steer, through the door: its command id is on her record, which is how she reaches it.
        # Before the approval opens: a steer supersedes an approval already waiting.
        ids["command_id"] = str((await steering.steer(s, ada, task_row.id, f"acme-steer-{tag}")).command_id)

        step = PlanStep(task_id=task_row.id, ordinal=0, title=f"acme-step-{tag}")
        s.add(step)
        await s.flush()
        gate = await gate_ops.open_approval(
            s, task_row, StepId(step.id), [("call_0", "Gmail", "gmail_send_email", f"acme-gate-{tag}")]
        )
        ids["gate_id"] = str(gate.id)
        knock = await s.scalar(select(InboxItem).where(InboxItem.gate_id == gate.id))
        assert knock is not None
        ids["item_id"] = str(knock.id)

        body = f"# Acme memo {tag}\n\nthe acme secret.\n".encode()
        sha = storage.sha256(body)
        manifest = {
            "entries": [
                {
                    "path": "/artifacts/acme.md",
                    "type": "document",
                    "title": f"acme-artifact-{tag}",
                    "sha256": sha,
                    "bytes": len(body),
                    "sources": [],
                    "agent": "",
                    "model": "",
                    "skill_pins": [],
                    "at": "2026-09-29T00:00:00Z",
                }
            ]
        }
        (art,) = await artifact_ops.harvest(
            s,
            task_row.id,
            step.id,
            Manifest.model_validate(manifest),
            {"/artifacts/acme.md": body},
            previews={"/artifacts/acme.md": b'{"kind": "markdown"}'},
            renditions={"/artifacts/acme.md": (b"%PDF-1.7 acme render", "application/pdf")},
        )
        ids["artifact_id"], ids["artifact_sha"] = str(art.id), sha
        verdict = VerificationReport(
            artifact_sha256=sha,
            suite=VerifierSuite.DOCUMENT,
            verdict=Verdict.PASS,
            producer_model="",
            producer_family=Family(""),
            at=dt.datetime(2026, 9, 29, tzinfo=dt.UTC),
        )
        s.add(
            Verification(
                artifact_id=art.id,
                verdict="pass",
                suite="document-suite",
                report=verdict.model_dump(mode="json"),
            )
        )

        conn = Connector(
            owner_actor_id=ada.id,
            name=f"acme-conn-{tag}",
            slug=f"acme{tag}",
            server_url=f"https://mcp.acme-{tag}.example.com",
        )
        plugin = PluginInstall(owner_actor_id=ada.id, name=f"acme-plugin-{tag}")
        s.add_all([conn, plugin])
        await s.commit()
        ids["connector_id"], ids["plugin_name"] = str(conn.id), plugin.name

    assert ids.keys() >= PARAM_KEYS, f"unseeded: {PARAM_KEYS - ids.keys()}"
    markers = {
        ids[k]
        for k in (*PARAM_KEYS, "a_id", "workspace_id", "a_email", "task_title", "plugin_name")
        if k != "seq"
    }
    markers |= {carol_email}
    markers |= {f"Acme Ada {tag}", f"acme-project-{tag}", f"acme-conv-{tag}", f"acme-line-{tag}"}
    markers |= {f"acme-channel-{tag}", f"acme-routine-{tag}", f"acme-{tag}.example.com"}
    markers |= {f"acme-capture-{tag}", f"acme-memory-{tag}", f"acme-key-{tag}", f"acme-gate-{tag}"}
    markers |= {f"acme-artifact-{tag}", f"acme-conn-{tag}", f"acme-step-{tag}", f"acme-installed-{tag}"}
    markers |= {f"acme-steer-{tag}"}
    yield Seeded(a, ids, viewers, markers)

    async with session_scope(platform=True) as s:  # a platform row: scratch does not sweep templates
        template_ids = select(BotTemplate.id).where(BotTemplate.share_slug == ids["slug"])
        await s.execute(delete(BotTemplateInstall).where(BotTemplateInstall.template_id.in_(template_ids)))
        await s.execute(delete(BotTemplate).where(BotTemplate.share_slug == ids["slug"]))
        await s.commit()


def _absent(ids: Vals) -> Vals:
    """Values that name nothing, shaped like A's."""
    out = dict(ids)
    for key, value in ids.items():
        if re.fullmatch(r"[0-9a-f-]{36}", value):
            out[key] = str(uuid.uuid4())
    for key in ("bot_name", "skill_name", "slug", "plugin_name"):
        out[key] = f"absent-{uuid.uuid4().hex[:8]}"
    out["seq"] = "987654"
    out["artifact_sha"] = "0" * 64
    out["task_title"] = f"absent-{uuid.uuid4().hex[:8]}"  # a search term that matches nothing
    return out


def _path_params(path: str) -> list[str]:
    return re.findall(r"\{([^}]+)\}", path)


def _url(path: str, vals: Vals) -> str:
    return re.sub(r"\{([^}]+)\}", lambda m: vals[key_for(path, m.group(1))], path)


async def _call(
    client: AsyncClient, method: str, url: str, headers: dict[str, str], body: Any = None, **params: str
) -> Response:
    async with asyncio.timeout(TIMEOUT_S):
        return await client.request(method, url, headers=headers, json=body, params=params or None)


def _masked(r: Response, vals: Vals) -> str:
    text = r.text
    for value in sorted({v for v in vals.values() if len(v) > 3}, key=len, reverse=True):
        text = text.replace(value, "<ref>")
    try:  # a JSON body compares by content, less the per-request correlation id
        return json.dumps(_without_request_id(json.loads(text)), sort_keys=True)
    except ValueError:
        return text


def _without_request_id(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_request_id(v) for k, v in value.items() if k != "request_id"}
    if isinstance(value, list):
        return [_without_request_id(v) for v in value]
    return value


# The upserts mint B-owned rows under A's names - after them B genuinely owns something by that
# name, so they are walked LAST, after every probe that hands B A's names.
_UPSERTS = {("PUT", "/v1/bots/{name}"), ("PUT", "/v1/skills/{name}")}


def _order(item: tuple[str, str, APIRoute]) -> tuple[int, str, str]:
    method, path, _ = item
    return {"GET": 0, "DELETE": 2}.get(method, 1), path, method


class _Walk:
    """One viewer's probes, and what they found."""

    def __init__(self, client: AsyncClient, seeded: Seeded, viewer: str) -> None:
        self.client, self.seeded, self.name = client, seeded, viewer
        self.viewer = seeded.viewers[viewer]
        self.ids, self.absent = seeded.ids, _absent(seeded.ids)
        self.leaks: list[str] = []
        self.walked = 0

    def leaked(
        self, text: str, supplied: Iterable[str] = (), route: tuple[str, str] | None = None
    ) -> list[str]:
        """A's markers in a body - less the values the viewer itself put in the request, and
        the viewer's own person-level facts."""
        allowed = set(supplied) | self.viewer.sees
        allowed |= {self.ids[k] for k in PERSON_LEVEL.get(self.name, {}).get(route or ("", ""), set())}
        return sorted(m for m in self.seeded.a_markers - allowed if m in text)

    async def param_route(self, method: str, path: str) -> None:
        """A's ids and ids that name nothing must be answered alike - and refused."""
        key, b = (method, path), self.viewer.headers
        body_fn = BODIES.get(key)
        extra = {"follow": "false"} if path.endswith("/events") else {}
        seen = await _call(
            self.client, method, _url(path, self.ids), b, body_fn(self.ids) if body_fn else None, **extra
        )
        none = await _call(
            self.client,
            method,
            _url(path, self.absent),
            b,
            body_fn(self.absent) if body_fn else None,
            **extra,
        )
        self.walked += 1
        if seen.status_code not in REFUSAL.get(key, {404}):
            self.leaks.append(
                f"{method} {path} answered {seen.status_code} to the {self.name}: {seen.text[:200]}"
            )
        elif seen.status_code != none.status_code:
            self.leaks.append(
                f"{method} {path}: A's id answers {seen.status_code} but an absent id answers "
                f"{none.status_code} - an existence oracle"
            )
        elif key not in _UPSERTS and _masked(seen, self.ids) != _masked(none, self.absent):
            self.leaks.append(
                f"{method} {path}: A's id is answered {_masked(seen, self.ids)[:200]} but an absent "
                f"id {_masked(none, self.absent)[:200]} - an existence oracle"
            )
        elif hits := self.leaked(
            seen.text, supplied={self.ids[key_for(path, p)] for p in _path_params(path)}
        ):
            self.leaks.append(f"{method} {path} leaked {hits} to the {self.name}")

    async def list_route(self, method: str, path: str, route: APIRoute) -> None:
        """A read with no path parameters: none of A's markers, bare or filtered by A's values -
        and filtered by A's values it answers exactly as filtered by values that name nothing, so
        a count or an aggregate (which carries no marker) cannot be an oracle either."""
        b = self.viewer.headers
        self.walked += 1
        bare = await _call(self.client, method, path, b)
        if bare.status_code >= 500:
            self.leaks.append(f"{method} {path} answered {bare.status_code}")
        if hits := self.leaked(bare.text, route=(method, path)):
            self.leaks.append(f"{method} {path} leaked {hits} to the {self.name}")
        refs = sorted({q.alias for q in route.dependant.query_params} & QUERY_REFS.keys())
        # each of A's values alone, then all together (a list may need two to answer at all)
        for aliases in [*([a] for a in refs), *([refs] if len(refs) > 1 else [])]:
            query = {alias: self.ids[QUERY_REFS[alias]] for alias in aliases}
            nothing = {alias: self.absent[QUERY_REFS[alias]] for alias in aliases}
            filtered = await _call(self.client, method, path, b, **query)
            none = await _call(self.client, method, path, b, **nothing)
            if filtered.status_code >= 500:
                self.leaks.append(f"{method} {path}?{aliases}=<A's> answered {filtered.status_code}")
            if hits := self.leaked(filtered.text, supplied=query.values(), route=(method, path)):
                self.leaks.append(f"{method} {path}?{aliases}=<A's> leaked {hits}")
            elif filtered.status_code != none.status_code or _masked(filtered, self.ids) != _masked(
                none, self.absent
            ):
                self.leaks.append(
                    f"{method} {path}?{aliases}=<A's> answers {filtered.status_code} "
                    f"{_masked(filtered, self.ids)[:160]} but filtered by nothing {none.status_code} "
                    f"{_masked(none, self.absent)[:160]} - an aggregate oracle"
                )

    async def cross_ref(self, label: str, method: str, path: str, body_fn: BodyFn) -> None:
        """A write whose body names one of A's rows: answered as for a row that does not exist."""
        b = self.viewer.headers
        if label in SKIP_CROSS_REF.get(self.name, set()):
            return
        url = path.format(own_task_id=self.viewer.own_task_id)
        seen = await _call(self.client, method, url, b, body_fn(self.ids))
        none = await _call(self.client, method, url, b, body_fn(self.absent))
        self.walked += 1
        if seen.status_code < 400:
            self.leaks.append(
                f"{method} {path} ({label}) accepted A's reference: {seen.status_code} {seen.text[:200]}"
            )
        elif seen.status_code != none.status_code or _masked(seen, self.ids) != _masked(none, self.absent):
            self.leaks.append(
                f"{method} {path} ({label}): A's reference answers {seen.status_code} "
                f"{_masked(seen, self.ids)[:160]} but a reference to nothing answers {none.status_code} "
                f"{_masked(none, self.absent)[:160]}"
            )

    async def websocket(self) -> None:
        """The WebSocket routes, answered as for a task that does not exist: the task stream closes
        4404 before accept to the viewer's header, and 4401 to a stream ticket the viewer minted for
        its OWN task presented on this path; the screen accepts, says only `bye{unauthorized}` and closes 4401 - with
        no ticket, and with a ticket the viewer minted for its OWN task presented on this path. Its
        process start is pinned in the past, so that ticket is refused for naming another task
        (`mismatch`), never as a fresh process's `stale` one."""
        self.walked += 1
        own = await self._own_screen_ticket()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(tickets, "PROCESS_STARTED", 0.0)
            for task_id, label in (
                (self.ids["task_id"], "A's task"),
                (self.absent["task_id"], "an absent task"),
            ):
                stream = await _ws_messages(f"/v1/tasks/{task_id}/stream", "follow=false", self.viewer.token)
                if stream != [{"type": "websocket.close", "code": 4404}]:
                    self.leaks.append(f"WS /v1/tasks/{{task_id}}/stream for {label} answered {stream}")
                ticketed = await _ws_messages(
                    f"/v1/tasks/{task_id}/stream", f"ticket={await self._own_stream_ticket()}&follow=false"
                )
                if ticketed != [{"type": "websocket.close", "code": 4401}]:
                    self.leaks.append(
                        f"WS /v1/tasks/{{task_id}}/stream with the viewer's own ticket answered {ticketed}"
                    )
                for query, what in (("", "no ticket"), (f"ticket={own}", "the viewer's own ticket")):
                    screen = await _ws_messages(f"/v1/tasks/{task_id}/screen", query)
                    if screen != _SCREEN_REFUSED:
                        self.leaks.append(
                            f"WS /v1/tasks/{{task_id}}/screen with {what} for {label} answered {screen}"
                        )

    async def _own_stream_ticket(self) -> str:
        """A single-use stream ticket the viewer mints for its own task through the real door."""
        minted = await _call(
            self.client, "POST", f"/v1/tasks/{self.viewer.own_task_id}/events/ticket", self.viewer.headers
        )
        return str((await _ok(minted))["ticket"])

    async def _own_screen_ticket(self) -> str:
        """A screen ticket the viewer mints for its own task through the real door. The walk runs
        with computer use off (its default), so the switch is on for this one call only."""
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(settings.computer_use, "enabled", True)
            minted = await _call(
                self.client,
                "POST",
                f"/v1/tasks/{self.viewer.own_task_id}/screen/ticket",
                self.viewer.headers,
                {"conn": "isolation1"},
            )
        return str((await _ok(minted))["ticket"])


@pytest.mark.parametrize("viewer", ["stranger", "self_elsewhere"])
async def test_workspace_b_cannot_see_or_touch_workspace_a(
    client: AsyncClient, seeded: Seeded, viewer: str
) -> None:
    """`stranger`: person B in B's own workspace. `self_elsewhere`: A herself, switched to another workspace
    she belongs to - the fence under test is the workspace's, not the person's."""
    walk = _Walk(client, seeded, viewer)
    for method, path, route in sorted(endpoints(), key=_order):
        key = (method, path)
        if key in EXEMPT or key in OWN_ONLY or key in CROSS_REF_ROUTES or key in _UPSERTS:
            continue
        if key in PERSON_LEVEL_SKIP.get(viewer, {}):
            continue
        if iso_params(route):
            await walk.param_route(method, path)
        else:
            await walk.list_route(method, path, route)
    for label, method, path, body_fn in [*CROSS_REFS, *CROSS_REFS_ON_OWN]:
        await walk.cross_ref(label, method, path, body_fn)
    # The one write with no reference to hand it: the viewer's read-all must not reach A's inbox
    # (checked in `_a_is_intact`).
    await _ok(await _call(client, "POST", "/v1/inbox/read-all", walk.viewer.headers))
    await walk.websocket()
    for method, path in sorted(_UPSERTS):
        await walk.param_route(method, path)

    assert not walk.leaks, f"{viewer} reached workspace A on {len(walk.leaks)} probe(s):\n" + "\n".join(
        walk.leaks
    )
    assert walk.walked > 120, f"walked only {walk.walked} routes"

    await _a_is_intact(client, seeded)


# The screen's refusal: accepted, one `bye`, then 4401 - and not a byte more.
_SCREEN_REFUSED = [
    {"type": "websocket.accept"},
    {"type": "websocket.send", "header": {"t": "bye", "reason": "unauthorized", "n": 0}},
    {"type": "websocket.close", "code": 4401},
]


async def _ws_messages(path: str, query: str, token: str | None = None) -> list[dict[str, Any]]:
    """Drive the ASGI app as a WebSocket client would (`query` is the URL's query string, `token`
    an Authorization header), and
    return what the server sent, up to its close: each message's type, a close's code and a binary
    message's decoded header."""
    app = create_app()
    sent: list[MutableMapping[str, Any]] = []
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})

    async def receive() -> dict[str, Any]:
        return await inbox.get()

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "websocket.close":
            await inbox.put({"type": "websocket.disconnect", "code": 1000})

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(b"host", b"test"), *([(b"authorization", f"Bearer {token}".encode())] if token else [])],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "subprotocols": [],
    }
    async with asyncio.timeout(TIMEOUT_S):
        await app(scope, receive, send)
    assert sent, f"{path}: the app sent nothing"
    out: list[dict[str, Any]] = []
    for message in sent:
        if message["type"] == "websocket.close":
            out.append({"type": message["type"], "code": message.get("code")})
            break
        if message["type"] == "websocket.send" and message.get("bytes") is not None:
            out.append({"type": message["type"], "header": decode(message["bytes"])[0]})
        else:
            out.append({"type": message["type"]})
    return out


async def _a_is_intact(client: AsyncClient, seeded: Seeded) -> None:
    """The positive control. A reaches every detail route B was refused (so every id the walk used
    is real), finds her rows on the lists B searched, and nothing B tried changed them."""
    ids, a = seeded.ids, seeded.a_headers
    reached: set[str] = set()
    for method, path, route in endpoints():
        params = iso_params(route)
        if method != "GET" or not params or (method, path) in EXEMPT or (method, path) in REFUSAL:
            continue
        extra = {"follow": "false"} if path.endswith("/events") else {}
        r = await _call(client, "GET", _url(path, ids), a, **extra)
        assert r.status_code == 200, f"A's own GET {path} answered {r.status_code}: {r.text[:200]}"
        reached |= {key_for(path, p) for p in params}
    for path, key in POSITIVE_LISTS.items():
        r = await _call(client, "GET", path.format(**ids), a)
        assert r.status_code == 200 and ids[key] in r.text, f"A's own {path} is missing her {key}"
        reached.add(key)
    members = await _call(client, "GET", f"/v1/projects/{ids['project_id']}/members", a)
    if ids["actor_id"] in members.text:
        reached.add("actor_id")
    invites = await _call(client, "GET", f"/v1/workspaces/{ids['workspace_id']}/invites", a)
    if ids["invite_id"] in invites.text:  # still pending: B's DELETE never reached it
        reached.add("invite_id")
    links = await _ok(await _call(client, "GET", f"/v1/projects/{ids['project_id']}/invites", a))
    if any(i["id"] == ids["project_invite_id"] and i["state"] == "live" for i in links):
        reached.add("project_invite_id")  # still live: B's DELETE never revoked it
    lines = await _ok(await _call(client, "GET", f"/v1/conversations/{ids['conversation_id']}/messages", a))
    if any(str(m["seq"]) == ids["seq"] for m in lines):
        reached.add("seq")
    record = await _call(client, "GET", f"/v1/tasks/{ids['task_id']}/events", a, follow="false")
    if ids["command_id"] in record.text and "steer.retracted" not in record.text:
        reached.add("command_id")  # on A's record, and B's retract never withdrew it
    async with session_scope(platform=True) as s:  # no route reads a phone back: A's row, still there
        if await s.get(PushDevice, uuid.UUID(ids["device_id"])) is not None:
            reached.add("device_id")  # B's DELETE never removed it
    assert reached >= PARAM_KEYS, f"a seeded id A herself cannot reach: {sorted(PARAM_KEYS - reached)}"

    task = await _ok(await _call(client, "GET", f"/v1/tasks/{ids['task_id']}", a))
    assert task["project_id"] == ids["project_id"] and not task.get("archived_at"), task
    assert task.get("title") != "b was here", "B renamed A's task"
    project = await _ok(await _call(client, "GET", f"/v1/projects/{ids['project_id']}", a))
    assert project["name"].startswith("acme-project-"), project
    unread = (await _ok(await _call(client, "GET", "/v1/inbox", a, unread="true")))["items"]
    assert ids["item_id"] in {i["id"] for i in unread}, "B's read-all reached A's inbox"
    gates = await _ok(await _call(client, "GET", "/v1/gates", a))  # the OPEN queue
    assert ids["gate_id"] in {g["id"] for g in gates}, "B resolved A's gate"
    assert task["status"] != "cancelled", task
    bot = await _ok(await _call(client, "GET", f"/v1/bots/{ids['bot_name']}", a))
    assert bot.get("title") != "b" and bot["status"] == "published", bot
    for resource in ("bots", "skills"):  # an upsert under A's name minted nothing in A's lineage
        versions = await _ok(
            await _call(client, "GET", f"/v1/{resource}/{ids[resource[:-1] + '_name']}/versions", a)
        )
        assert len(versions) == 1, versions
    members = await _ok(await _call(client, "GET", f"/v1/projects/{ids['project_id']}/members", a))
    assert ids["b_email"] not in json.dumps(members), "B joined A's project"
    memory = await _ok(await _call(client, "GET", f"/v1/memories/{ids['memory_id']}", a))
    assert memory["text"].startswith("acme-memory-") and memory["status"] == "active", memory
    routine = await _ok(await _call(client, "GET", f"/v1/routines/{ids['routine_id']}", a))
    assert routine["name"].startswith("acme-routine-") and routine["status"] == "active", routine
    connector = await _ok(await _call(client, "GET", f"/v1/connectors/{ids['connector_id']}", a))
    assert connector["name"].startswith("acme-conn-"), connector
    allowances = await _ok(await _call(client, "GET", "/v1/egress-allowances", a))
    assert [x["revoked_at"] for x in allowances if x["id"] == ids["allowance_id"]] == [None], allowances
    grants = await _ok(await _call(client, "GET", "/v1/device/grants", a))
    assert [g["kind"] for g in grants if g["id"] == ids["grant_id"]] == ["docker"], "B revoked A's grant"
    assert len(grants) == 1, "B granted A's task"
    captures = await _ok(await _call(client, "GET", "/v1/capture", a))
    assert [c["promoted_task_id"] for c in captures if c["id"] == ids["capture_id"]] == [None], captures
    conversation = await _ok(await _call(client, "GET", f"/v1/conversations/{ids['conversation_id']}", a))
    assert conversation["title"].startswith("acme-conv-") and not conversation.get("archived"), conversation
