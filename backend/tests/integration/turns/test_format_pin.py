# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A format maker's skill is pinned when the plan is written: a version published mid-step never
reaches the step's retry, and the plan says which version it runs."""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.models import tenant
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.skill import SkillRecord, SkillStatus
from prismoid.db.session import session_scope
from prismoid.operations import planning_steps
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import equip
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepSpec
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_a_version_published_mid_step_never_reaches_its_retry() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Pinner")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal="the renewals memo as a Word file")
        maker = StepSpec(
            agent=AgentName("maker"),
            contract=Contract(
                goal="write the memo",
                acceptance=[],
                output=OutputRef(type=ArtifactType.DOCUMENT, title="Memo", filename="memo.docx"),
            ),
        )
        [row] = await planning_steps.write_steps(
            session, task, [(StepId(uuid.uuid4()), maker)], [], base_ordinal=0
        )
        planned = StepSpec.model_validate(row.spec)
        assert [p.ref() for p in planned.skill_pins] == ["docx@1"], "the plan shows the pin"

        head = await SkillRecord.at_version(session, "docx", 1)
        assert head is not None
        session.add(
            SkillRecord(
                workspace_id=tenant.workspace_of(session),
                name="docx",
                version=4,
                status=SkillStatus.PUBLISHED.value,
                origin=head.origin,
                manifest=head.manifest,
                body_sha256=head.body_sha256,
                body_key=head.body_key,
                eval_status=head.eval_status,
            )
        )
        await session.flush()
        published = await SkillRecord.head(session, "docx")
        assert published is not None and published.version == 4

        step = await session.get(PlanStep, row.id)
        assert step is not None
        retried = await equip.settle_skill_pins(session, task, step, StepSpec.model_validate(step.spec))
        assert [p.ref() for p in retried.skill_pins] == ["docx@1"]
        await session.rollback()
