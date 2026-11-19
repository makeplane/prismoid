# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skills - user-editable: draft → test → publish. Publishing needs green evals.

`list_skills` and `list_versions` are the read side (the menu a router chooses from, one skill's
history); create/update-draft and the green-evals publish gate are the write side. The
routing-eval `test` runs in-process in the CLI (the API does no model work), so there is no
`test` route. Every route requires an Actor - auth is never a later feature.
"""

from __future__ import annotations

import base64
import binascii

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.skills import SkillCreateIn, SkillOut, SkillPushIn, skill_out
from prismoid.errors import NotFound, ValidationFailed
from prismoid.operations import skills as ops


def _decode_file(path: str, content_b64: str) -> tuple[str, bytes]:
    """base64 → bytes **at the edge**, mirroring `tasks._decode`: a malformed body is a 422
    naming the file, not a bare `binascii.Error` surfacing as a 500 from deep inside an operation."""
    try:
        return path, base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationFailed(
            f"{path}: content is not valid base64", fields={path: "not valid base64"}
        ) from None


async def create_skill(body: SkillCreateIn, actor: ActorDep, session: SessionDep) -> SkillOut:
    # `files` forwards exactly as in `update_draft` below - the POST used to drop them silently,
    # so a first push with bundled assets arrived bare and only a later PUT carried them.
    files = [_decode_file(f.path, f.content_b64) for f in body.files]
    record = await ops.push(
        session,
        actor_id=actor.id,
        name=str(body.name),
        skill_md=body.skill_md,
        cases_yaml=body.cases_yaml,
        files=files,
    )
    return skill_out(record)


async def list_skills(actor: ActorDep, session: SessionDep) -> list[SkillOut]:
    heads = await ops.list_skills(session)
    return [skill_out(r) for r in heads]


async def get_skill(name: str, actor: ActorDep, session: SessionDep) -> SkillOut:
    versions = await ops.versions(session, name)
    if not versions:
        raise NotFound(f"no skill named {name!r}")
    published = [v for v in versions if v.status == "published"]
    return skill_out(published[0] if published else versions[0])  # head, else newest (a draft-only skill)


async def update_draft(name: str, body: SkillPushIn, actor: ActorDep, session: SessionDep) -> SkillOut:
    files = [_decode_file(f.path, f.content_b64) for f in body.files]
    record = await ops.push(
        session, actor_id=actor.id, name=name, skill_md=body.skill_md, cases_yaml=body.cases_yaml, files=files
    )
    return skill_out(record)


async def publish_skill(name: str, actor: ActorDep, session: SessionDep) -> SkillOut:
    record = await ops.publish(session, actor_id=actor.id, name=name)  # 409 if the draft's evals are red
    return skill_out(record)


async def list_versions(name: str, actor: ActorDep, session: SessionDep) -> list[SkillOut]:
    records = await ops.versions(session, name)
    if not records:
        raise NotFound(f"no skill named {name!r}")
    return [skill_out(r) for r in records]
