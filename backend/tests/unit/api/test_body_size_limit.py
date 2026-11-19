# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The API reads no request body past its route's cap: 1 MiB, or `max_body_bytes` on the few
routes whose body carries files. A declared length over the cap is refused unread, a streamed body
is cut off the moment it passes it, and either way the answer is the error envelope's 413. The
free-text request fields carry their own ceilings too."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError

from prismoid.api import errors, middleware
from prismoid.api.serializers.channels import ChannelMessageIn
from prismoid.api.serializers.skills import SkillPushIn
from prismoid.api.serializers.tasks import MAX_INPUT_BASE64_CHARS, TaskInputIn
from prismoid.conf import settings
from prismoid.schemas.inputs import MAX_INPUT_BYTES

_CAP = 1024


class _Body(BaseModel):
    text: str


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(middleware.BodySizeLimit)
    middleware.install(app)
    errors.install(app)

    @app.post("/echo")
    async def echo(body: _Body) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]  # a route
        return {"len": len(body.text)}

    return app


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(middleware, "BODY_BYTES", _CAP)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
        yield c


async def test_a_body_under_the_cap_reaches_the_route(client: AsyncClient) -> None:
    answered = await client.post("/echo", json={"text": "x" * 100})
    assert answered.status_code == 200 and answered.json() == {"len": 100}


async def test_a_declared_length_over_the_cap_is_refused_unread(client: AsyncClient) -> None:
    answered = await client.post("/echo", json={"text": "x" * (_CAP * 2)})
    assert answered.status_code == 413
    assert answered.json()["error"]["code"] == "payload_too_large"
    assert answered.json()["error"]["request_id"] != "-"


async def test_a_streamed_body_is_cut_off_past_the_cap(client: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"text": "'
        for _ in range(8):
            yield b"x" * 512
        yield b'"}'

    answered = await client.post("/echo", content=chunks(), headers={"content-type": "application/json"})
    assert answered.status_code == 413
    assert answered.json()["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/auth/signup",
        "/v1/auth/login",
        "/v1/auth/forgot-password",
        "/v1/auth/reset-password",
        "/v1/channels/0b7c/messages",
        "/v1/channels/inbound/email",
        "/v1/tasks/0b7c/steer",
        "/v1/tasks/0b7c/continue",
        "/v1/capture",
        "/v1/memories",
        "/v1/conversations/0b7c/messages",
        "/v1/bots/helper/test",
        "/v1/channels/0b7c",
        "/v1/gates/0b7c/resolve",
    ],
)
def test_a_route_that_carries_no_file_takes_a_machine_sized_body(path: str) -> None:
    """FastAPI reads and parses a body before the route's dependencies authenticate anyone, so a
    stranger with no account reaches every route's body cap, not only the doors'."""
    assert middleware.body_cap(path) == middleware.BODY_BYTES
    assert settings.api.max_body_bytes // 64 >= middleware.BODY_BYTES


@pytest.mark.parametrize(
    "path",
    [
        "/v1/tasks",
        "/v1/tasks/0b7c/follow_up",
        "/v1/skills",
        "/v1/skills/report",
        "/v1/bots",
        "/v1/bots/helper",
        "/v1/artifacts/0b7c/revise",
    ],
)
def test_a_route_whose_body_carries_files_keeps_the_attachment_sized_cap(path: str) -> None:
    assert middleware.body_cap(path) == settings.api.max_body_bytes


async def test_a_route_refuses_a_body_only_a_file_route_would_take(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(middleware, "BODY_BYTES", _CAP)
    app = _app()

    @app.post("/v1/tasks")
    async def create(body: _Body) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]  # a route
        return {"len": len(body.text)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        task = await c.post("/v1/tasks", json={"text": "x" * (_CAP * 2)})
        echo = await c.post("/echo", json={"text": "x" * (_CAP * 2)})
    assert task.status_code == 200, "a task's attachments ride the general cap (80 MiB)"
    assert echo.status_code == 413


def test_the_door_cap_fits_the_largest_honest_channel_line() -> None:
    """A 60,000-character line of emoji, JSON-escaped as surrogate pairs (12 bytes each), plus the
    rest of the message, still fits the door."""
    line = ChannelMessageIn(message="\N{GRINNING FACE}" * 60_000, sender="s" * 255, thread="t" * 120)
    assert len(json.dumps(line.model_dump()).encode()) < middleware.BODY_BYTES


def test_the_api_installs_the_guard() -> None:
    from prismoid.api.app import create_app

    assert middleware.BodySizeLimit in [m.cls for m in create_app().user_middleware]


def test_the_cap_fits_a_task_with_every_attachment_at_its_largest() -> None:
    assert settings.api.max_body_bytes > MAX_INPUT_BASE64_CHARS * 10
    assert MAX_INPUT_BASE64_CHARS >= 4 * MAX_INPUT_BYTES // 3


def test_an_attachment_past_its_base64_ceiling_is_a_422_not_a_parse() -> None:
    with pytest.raises(ValidationError):
        TaskInputIn(filename="a.txt", content="A" * (MAX_INPUT_BASE64_CHARS + 1))


def test_authored_text_has_a_ceiling() -> None:
    with pytest.raises(ValidationError):
        SkillPushIn(skill_md="x" * (settings.api.max_text_chars + 1))
    assert SkillPushIn(skill_md="---\nname: a\n---\n").skill_md
