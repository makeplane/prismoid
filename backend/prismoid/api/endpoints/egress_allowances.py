# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Egress allowances - the owner's standing "always allow" hosts for the browser.

Thin: auth → validate → one operation call → serialize. ≤20 lines each, no business
logic, no transaction.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.common import OkOut
from prismoid.api.serializers.egress_allowances import (
    EgressAllowanceCreateIn,
    EgressAllowanceOut,
    egress_allowance_out,
)
from prismoid.operations import egress_allowances as ops


async def list_egress_allowances(actor: ActorDep, session: SessionDep) -> list[EgressAllowanceOut]:
    return [egress_allowance_out(r) for r in await ops.list_allowances(session, actor)]


async def add_egress_allowance(
    body: EgressAllowanceCreateIn, actor: ActorDep, session: SessionDep
) -> EgressAllowanceOut:
    return egress_allowance_out(await ops.add_allowance(session, actor, body.host))


async def revoke_egress_allowance(allowance_id: UUID, actor: ActorDep, session: SessionDep) -> OkOut:
    await ops.revoke_allowance(session, actor, allowance_id)
    return OkOut(ok=True)
