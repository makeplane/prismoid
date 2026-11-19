# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bots - request bodies and responses. `*In` = request, `*Out` = response.

Two input shapes for one resource, deliberately. The CLI holds a `BOT.md` file and sends it
verbatim, exactly as the skills lane does. The web holds a form - a capability dropdown, tool
checkboxes - and sending it as hand-assembled YAML would put a second renderer in TypeScript and
a second set of quoting bugs with it.

So a structured body becomes a `BotSource` here and `operations/bots.py` renders it into the same
`BOT.md` bytes before walking it through the one format door. The rendering lives down there and
not here because **views never touch the engine** - `api` may not import `prismoid.ai`,
and `tools/audit.sh` greps for exactly that.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.conf import settings
from prismoid.db.models.bot import BotRecord
from prismoid.schemas.bots import (
    MAX_DESCRIPTION_CHARS,
    MAX_JOB_CHARS,
    MAX_ROUTING_PHRASES,
    BotCase,
    BotSource,
    GoldenBotCase,
)
from prismoid.schemas.ids import BotName


class BotCaseIn(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    expect: str = Field(min_length=1, max_length=64)


class GoldenCaseIn(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    must_contain: list[str] = Field(default_factory=list, max_length=8)
    inputs: list[str] = Field(default_factory=list, max_length=8)


class BotPushIn(BaseModel):
    """An author pushing an edited bot. Either the raw `BOT.md` (the CLI) or the fields (the web).

    `bot_md` wins when both are present: a file the author typed is more authoritative than a
    form the client assembled from it.
    """

    model_config = ConfigDict(frozen=True)

    bot_md: str | None = Field(default=None, max_length=settings.api.max_text_chars)
    cases_yaml: str | None = Field(default=None, max_length=settings.api.max_text_chars)

    # the structured half
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    # The three small questions: what it does, when it steps in, what it refuses.
    # When `description` is absent these compose it server-side; the author never types grammar.
    job: str | None = Field(default=None, max_length=MAX_JOB_CHARS)
    use_when: list[str] = Field(default_factory=list, max_length=MAX_ROUTING_PHRASES)
    not_for: list[str] = Field(default_factory=list, max_length=MAX_ROUTING_PHRASES)
    instructions: str | None = Field(default=None, max_length=settings.api.max_text_chars)
    capability: str | None = Field(default=None, max_length=80)
    produces: str | None = Field(default=None, max_length=80)
    tools: list[str] = Field(default_factory=list, max_length=8)
    skills: list[str] = Field(default_factory=list, max_length=8)
    connectors: list[str] = Field(default_factory=list, max_length=8)
    connector_why: dict[str, str] = Field(default_factory=dict)  # author's reason per slug
    can_ask_user: bool = True
    cases: list[BotCaseIn] = Field(default_factory=list, max_length=20)
    golden: list[GoldenCaseIn] = Field(default_factory=list, max_length=5)  # advisory nightly cases


class BotCreateIn(BotPushIn):
    """Creating a bot (POST) carries its name in the body; a push to an existing one (PUT) takes
    the name from the path. `project_id` is set once, at birth - a bot does not move between
    scopes, because its name is only unique within one."""

    name: BotName
    project_id: UUID | None = None


class BotFaceIn(BaseModel):
    """The face PATCH - the projects wire grammar exactly: optional values plus explicit clear
    flags, because a PATCH cannot say null and "leave it alone" with one field."""

    model_config = ConfigDict(frozen=True)

    icon: str | None = Field(default=None, max_length=80)
    icon_hue: int | None = Field(default=None, ge=0, le=7)
    title: str | None = Field(default=None, max_length=120)
    clear_icon: bool = False
    clear_hue: bool = False
    clear_title: bool = False
    # The work dials: ask | auto | never, and the per-task cap in dollars.
    work_mode: str | None = Field(default=None, pattern=r"^(ask|auto|never)$")
    task_budget_usd: float | None = Field(default=None, ge=0, le=500)
    # The threads dial: one chat, or a chat per topic.
    threads: str | None = Field(default=None, pattern=r"^(one|many)$")


class BotOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: int
    status: str  # draft | published | archived
    origin: str  # user | imported
    description: str  # the routing surface One reads
    # The structured fields the description was composed from, when it was - the
    # web edits these; verbatim-prose rows carry them empty.
    job: str
    use_when: list[str]
    not_for: list[str]
    capability: str
    produces: str
    tools: list[str]
    skills: list[str]
    connectors: list[str]
    connector_why: dict[str, str]
    can_ask_user: bool
    eval_status: str  # untested | green | red - publishing needs green
    # The face (the Project precedent): stored verbatim, rendered by the web's icon vocabulary.
    icon: str | None
    icon_hue: int | None
    title: str | None
    # Sidebar presence (the messenger's pin law): shown in the left nav only while true.
    pinned: bool
    # The work dials: may the bot start work from a conversation by itself, and
    # what one chat-born task may cost.
    work_mode: str
    task_budget_usd: str
    # The threads dial: `one` (the bot is its one chat) or `many`.
    threads: str
    # The authored selection cases - part of the bot, not of any one eval run. The web's
    # editor must carry these forward on save (a draft without cases can never go green),
    # and a save that silently dropped them was the live 422 this field closes.
    cases: list[dict[str, str]]
    # The advisory nightly cases (the golden lane) - the Editor echoes
    # these forward exactly as it does `cases`, so a web save never silently drops them.
    golden: list[dict[str, Any]]
    # The selection report behind that verdict, verbatim - per-case results, so a red draft's
    # page can say WHICH goal was lost to whom instead of just wearing the color.
    eval_report: dict[str, Any]
    # The authored judgment itself. In the response because the web's detail page IS the
    # authoring surface - unlike a built-in agent's prompt (deliberately absent from AgentView),
    # a bot's instructions belong to the user reading them.
    instructions: str
    instructions_sha256: str
    project_id: UUID | None
    owner_actor_id: UUID
    created_at: dt.datetime


def bot_out(record: BotRecord) -> BotOut:
    """The row → the response. The routing surface is lifted out of the manifest JSONB so a
    caller never reaches into a nested blob (the `skill_out` pattern)."""
    manifest: dict[str, Any] = record.manifest or {}
    return BotOut(
        name=record.name,
        version=record.version,
        status=record.status,
        origin=record.origin,
        description=str(manifest.get("description", "")),
        job=str(manifest.get("job", "")),
        use_when=[str(p) for p in manifest.get("use_when", [])],
        not_for=[str(p) for p in manifest.get("not_for", [])],
        capability=str(manifest.get("capability", "")),
        produces=str(manifest.get("produces", "")),
        tools=[str(t) for t in manifest.get("tools", [])],
        skills=[str(s) for s in manifest.get("skills", [])],
        connectors=[str(c) for c in manifest.get("connectors", [])],
        connector_why={str(k): str(v) for k, v in (manifest.get("connector_why") or {}).items()},
        can_ask_user=bool(manifest.get("can_ask_user", True)),
        eval_status=record.eval_status,
        icon=record.icon,
        icon_hue=record.icon_hue,
        title=record.title,
        pinned=record.pinned,
        work_mode=record.work_mode,
        task_budget_usd=f"{float(record.task_budget_usd):.2f}",
        threads=record.threads,
        cases=[
            {"goal": str(c.get("goal", "")), "expect": str(c.get("expect", record.name))}
            for c in manifest.get("cases", [])
        ],
        golden=[dict(g) for g in manifest.get("golden", [])],
        eval_report=record.eval_report or {},
        instructions=record.instructions,
        instructions_sha256=record.instructions_sha256,
        project_id=record.project_id,
        owner_actor_id=record.owner_actor_id,
        created_at=record.created_at,
    )


def to_source(body: BotPushIn) -> BotSource:
    """The request body → the value `operations` renders and validates.

    A pure field mapping, nothing more: every value here is checked for real by the format door a
    moment later, and duplicating those rules at the edge would be a second place to keep right.
    The edge's job is shape; the door's job is doctrine.
    """
    return BotSource(
        bot_md=body.bot_md,
        cases_yaml=body.cases_yaml or "",
        description=body.description,
        job=body.job,
        use_when=tuple(body.use_when),
        not_for=tuple(body.not_for),
        instructions=body.instructions,
        capability=body.capability,
        produces=body.produces,
        tools=tuple(body.tools),
        skills=tuple(body.skills),
        connectors=tuple(body.connectors),
        connector_why=dict(body.connector_why),
        can_ask_user=body.can_ask_user,
        cases=tuple(BotCase(goal=c.goal, expect=c.expect) for c in body.cases),
        golden=tuple(
            GoldenBotCase(goal=g.goal, must_contain=tuple(g.must_contain), inputs=tuple(g.inputs))
            for g in body.golden
        ),
    )


class BotJobOut(BaseModel):
    """A 202: the authoring work was enqueued on the worker (Rule 5 keeps model
    calls out of the API process). The caller polls the bot itself: a new version appears
    (draft) or `eval_status` changes (test)."""

    model_config = ConfigDict(frozen=True)

    job: str


class BotPinIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    pinned: bool


class BotRunOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    goal: str
    title: str
    state: str
    attempt: int
    cost_usd: str
    at: str


class BotConnectorAccessOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    slug: str
    why: str
    display_name: str
    catalog: bool
    status: str  # connected | needs_auth | disabled | needs_connect | custom
    connector_id: str | None
    tools_allow: int
    tools_ask: int
    tools_off: int
    tool_policies: dict[str, str]


class BotAccessOut(BaseModel):
    """The access tab's payload - the connector half; tools/skills/ask ride BotOut."""

    model_config = ConfigDict(frozen=True)

    connectors: list[BotConnectorAccessOut]


class BotStatsOut(BaseModel):
    """The stats strip. Money rides as strings, the ledger's wire convention."""

    model_config = ConfigDict(frozen=True)

    spend_usd_total: str
    spend_usd_30d: str
    runs: int
    last_run_at: dt.datetime | None
    memory_count: int
    routine_count: int


class BotArtifactOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    task_id: str
    title: str
    filename: str
    type: str
    verdict: str | None
    goal: str
    at: str
