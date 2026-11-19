# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`sweep_channels` - the reach lane's minute drain (amended ADR-057).

One arm now: Telegram poll channels drain `getUpdates` into their thread conversations through
the ONE inbound walk. The queued-inbound belt is gone with the ledger it drained - a
conversation never queues talk behind work. `PRISMOID_CHANNELS_ENABLED=false`
stands the whole sweep down this tick, the routines kill-switch semantics.
"""

from __future__ import annotations

import datetime as dt

import logfire

from prismoid.jobs import durable


@durable.schedule("* * * * *")  # every minute
@durable.job("sweep_channels")
async def sweep_channels(scheduled: dt.datetime, actual: dt.datetime) -> None:
    del scheduled, actual
    from prismoid.conf import settings

    if not settings.channels.enabled:
        return
    kinds = {k.strip() for k in settings.channels.kinds.split(",") if k.strip()}
    if "telegram" not in kinds:
        return

    from prismoid.db.models import tenant
    from prismoid.db.models.bot_channel import BotChannel
    from prismoid.db.session import session_scope
    from prismoid.operations import channels as channel_ops
    from prismoid.schemas.channels import ChannelKind

    try:
        async with session_scope(platform=True) as session:  # cross-workspace roster
            for row in await BotChannel.all_active(session):
                if row.kind != ChannelKind.TELEGRAM.value:
                    continue
                tenant.bind(session, row.workspace_id)  # each drain acts for its OWN channel's workspace
                try:
                    await channel_ops.telegram_drain(session, row)
                except Exception:
                    # One channel's bad minute never starves the channels after it (found in
                    # review: one raising drain ended the whole sweep's tick).
                    await session.rollback()
                    logfire.exception("telegram drain failed for channel {id}", id=str(row.id))
    except Exception:
        logfire.exception("sweep_channels failed; next minute retries")
