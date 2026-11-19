# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Models - the catalog, readable by any authenticated actor.

The admin surface owns mutation; this read exists for pickers - the fleet seat chooser
needs to know what is flyable. Enabled rows only: what the computer can fly today, not the
whole ledger of what it has ever known.
"""

from __future__ import annotations

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.models import ModelOut, model_out
from prismoid.operations import models as ops


async def list_models(actor: ActorDep, session: SessionDep) -> list[ModelOut]:
    rows = await ops.list_models(session)
    return [model_out(r) for r in rows if r.enabled]
