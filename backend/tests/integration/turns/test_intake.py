# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Intake reads real turns: a Project task born in a channel's conversation, asked once, then continued; and
a thread whose turns come from an API key and the app, each with its own attachments."""

from __future__ import annotations

import base64
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient

from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import channels as channel_ops
from prismoid.operations import chat as chat_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.turn.intake import Bypass, intake
from prismoid.schemas.inputs import RawInput
from prismoid.schemas.lead import LeadEndingKind, LeadFacts, TurnOrigin
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import TaskStatus
from tests.support.chat_cast import an_owner, published_bot

pytestmark = pytest.mark.integration


async def test_intake_builds_the_facts_of_a_real_turn_and_its_follow_up() -> None:
    async with session_scope() as session:
        owner = await an_owner(session)
        project = (await project_ops.create(session, owner, name="Quarterly")).project
        name = await published_bot(session, owner)
        channel, _token = await channel_ops.create_channel(
            session, actor=owner, bot_name=name, kind="webhook"
        )
        conversation = await chat_ops.open_conversation(session, owner, bot_name=name)
        conversation.channel_id = channel.id
        await session.flush()
        task = await task_ops.create_task(
            session,
            owner,
            goal="Summarise the attached scan as a memo",
            project_id=project.id,
            origin_conversation_id=conversation.id,
            deliverable="document",
            deliverable_format="pdf",
            budget={"usd": "2.00"},
            inputs=[RawInput(filename="scan.pdf", data=b"%PDF-1.4", media_type="application/pdf")],
        )
        await session.commit()

        first = await intake(session, task)
        assert first.bypass is Bypass.DIAL
        assert first.facts.origin is TurnOrigin.CHANNEL and not first.facts.unattended
        assert first.facts.deliverable is ArtifactType.DOCUMENT and first.facts.deliverable_format == "pdf"
        # the Lead's rung opens a PDF itself (sidecar), so intake no longer counts it unread; it rides `inputs`
        assert first.facts.inputs_unread == 0 and [i.filename for i in first.inputs] == ["scan.pdf"]
        assert first.facts.budget_left_usd == Decimal("2.00")
        assert not first.facts.asked

        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        task.transition(TaskStatus.DELIVERED)
        await session.commit()
        await task_ops.continue_task(session, owner, task.id, goal="thanks, that is all")

        later = await intake(session, task)
        assert later.bypass is None, "a carried dial is not this turn's"
        assert later.facts.turn == 2 and later.facts.origin is TurnOrigin.WEB
        assert later.facts.deliverable is None and not later.facts.asked
        assert LeadEndingKind.REPLY in later.facts.allowed_endings()


async def _settle(task_id: str) -> None:
    async with session_scope(platform=True) as session:
        task = await session.get(Task, uuid.UUID(task_id))
        assert task is not None
        if task.status != TaskStatus.PLANNING:
            task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        task.transition(TaskStatus.DELIVERED)
        await session.commit()


async def _facts(task_id: str) -> LeadFacts:
    async with session_scope(platform=True) as session:
        task = await session.get(Task, uuid.UUID(task_id))
        assert task is not None
        return (await intake(session, task)).facts


def _png(name: str) -> dict[str, str]:
    """An attachment the Lead's rung never opens, so intake counts it unread (it reads a PDF itself)."""
    return {
        "filename": name,
        "content": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
        "media_type": "image/png",
    }


async def test_each_turn_reads_its_own_credential_and_attachments(client: AsyncClient) -> None:
    signed = await client.post(
        "/v1/auth/signup",
        json={
            "email": f"in{uuid.uuid4().hex[:12]}@example.com",
            "name": "Ada",
            "password": "a-long-enough-password",
        },
    )
    web = {"Authorization": f"Bearer {signed.json()['token']}"}
    key = (await client.post("/v1/auth/api-keys", headers=web, json={"name": "script"})).json()["key"]
    api = {"Authorization": f"Bearer {key}"}
    created = await client.post(
        "/v1/tasks", headers=api, json={"goal": "Read the scan", "inputs": [_png("scan.png")]}
    )
    assert created.status_code == 202, created.text
    task_id = str(created.json()["task_id"])
    first = await _facts(task_id)
    assert first.origin is TurnOrigin.API and first.inputs_unread == 1

    for headers, body, origin, unread in (
        (web, {"goal": "and this one", "inputs": [_png("later.png")]}, TurnOrigin.WEB, 1),
        (api, {"goal": "thanks"}, TurnOrigin.API, 0),
    ):
        await _settle(task_id)
        sent = await client.post(f"/v1/tasks/{task_id}/follow_up", headers=headers, json=body)
        assert sent.status_code == 202, sent.text
        facts = await _facts(task_id)
        assert (facts.origin, facts.inputs_unread) == (origin, unread), "only this turn's own files count"
