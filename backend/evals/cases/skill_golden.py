# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The per-skill golden lane (ADR-015) - end-to-end cases a skill SHIPS WITH.

`just evals --suite skill-golden` collects every published head's `golden:` cases (stored in the
manifest by `_write_version`) and runs them through the SAME harness as the main golden suite:
real tasks, real verification, `must_contain` against the delivered bytes. **Advisory, on the
nightly cadence - never the 409 publish gate**: gating publish on multi-minute real-money runs
would kill the sub-minute skill edit loop, and that trade is recorded in ADR-015 so nobody
"fixes" it.
"""

from __future__ import annotations

from evals.cases.golden import Golden


async def load_cases() -> list[Golden]:
    """Published heads' golden cases → `Golden` values the run harness already speaks."""
    from prismoid.db.models.skill import SkillRecord
    from prismoid.db.session import session_scope
    from prismoid.testutils.tenancy import member

    cases: list[Golden] = []
    async with session_scope() as session:
        # Heads resolve inside a workspace (ADR-061). A scratch, empty one reads the platform
        # builtins - what the eval user, whose workspace owns no skills, can run; the scope's
        # rollback drops it.
        await member(session, "skill-golden-evals")
        for record in await SkillRecord.published_heads(session):
            manifest = record.manifest or {}
            for i, raw in enumerate(manifest.get("golden", []), start=1):
                if not isinstance(raw, dict) or not raw.get("goal"):
                    continue
                cases.append(
                    Golden(
                        name=f"{record.name}-golden-{i}",
                        goal=str(raw["goal"]),
                        inputs=tuple(str(p) for p in raw.get("inputs", [])),
                        must_contain=tuple(str(m) for m in raw.get("must_contain", [])),
                        skill=record.name,
                    )
                )
    return cases
