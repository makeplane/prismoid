# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tamper test, against the stored row (promoted by ADR-008).

`tests/unit/ai/verification/test_signing.py` proves the crypto refuses every forgery in memory. This proves the
same thing where it matters - the database. A signed FAIL, quietly flipped to `pass` with the one
line of SQL an attacker (or a careless migration) would write, must come back
`signature_valid: false` when revived from JSONB through the same path the API serves.

The edit succeeds. The lie does not.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.verification import signing
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.verification import Verification
from prismoid.db.session import session_scope
from prismoid.operations import tasks as task_ops
from prismoid.operations import verification as verification_ops
from prismoid.schemas.model_class import Family
from prismoid.schemas.verification import (
    Check,
    CheckKind,
    CheckStatus,
    Verdict,
    VerificationReport,
    VerifierSuite,
)
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Just a session. Cleanup is autouse in `tests/conftest.py`, scoped to what this test made -
    a suite that nukes `actors` also nukes the developer's login."""
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


def a_failing_report(sha256: str) -> VerificationReport:
    return VerificationReport(
        artifact_sha256=sha256,
        suite=VerifierSuite.DOCUMENT,
        checks=[
            Check(
                name="numbers:grounded",
                status=CheckStatus.FAIL,
                kind=CheckKind.SUBSTANTIVE,
                detail="1 ungrounded",
                expected="every figure verbatim in a source, or derived from it in one step",
                actual="in no source: 99.7M",
            )
        ],
        judge=None,
        verdict=Verdict.FAIL,
        producer_model="anthropic:small",
        producer_family=Family("anthropic"),
        at=utcnow(),
    )


async def test_a_verdict_flipped_in_the_database_is_detected(db: AsyncSession, actor: Actor) -> None:
    task = await task_ops.create_task(db, actor, goal="a memo, checked")
    artifact = Artifact(
        id=uuid.uuid4(),
        lineage_id=uuid.uuid4(),
        task_id=task.id,
        type="document",
        title="a memo",
        sha256="a" * 64,
        bytes=0,
        storage_key="sha256/" + "a" * 64,
        provenance={},
    )
    db.add(artifact)

    # A legitimate FAIL, over the real row, signed with the real key. Everything about it is
    # honest except that we chose the verdict - because the attack worth testing is not "make a
    # pass look like a pass", it is *turn a fail into a pass*, quietly.
    report = a_failing_report(artifact.sha256)
    signed = signing.sign(report)
    db.add(
        Verification(
            id=uuid.uuid4(),
            artifact_id=artifact.id,
            verdict=report.verdict.value,
            suite=report.suite.value,
            producer_family=str(report.producer_family),
            verifier_family=None,
            report=report.model_dump(mode="json"),
            signature=signed.signature,
            public_key=signed.public_key,
            algorithm=signed.algorithm,
        )
    )
    await db.commit()

    honest = await verification_ops.latest_for(db, artifact.id)
    assert honest is not None
    assert honest.report.verdict is Verdict.FAIL
    assert verification_ops.signature_valid(honest) is True, "a fresh signature must verify"

    # The single most valuable line of SQL an attacker could write.
    await db.execute(
        text(
            "update verifications set report = jsonb_set(report, '{verdict}', '\"pass\"') "
            "where artifact_id = :aid"
        ),
        {"aid": artifact.id},
    )
    await db.commit()

    tampered = await verification_ops.latest_for(db, artifact.id)
    assert tampered is not None
    assert tampered.report.verdict is Verdict.PASS, "the row now claims pass - that is the lie"
    assert verification_ops.signature_valid(tampered) is False, "…and the signature calls it"
