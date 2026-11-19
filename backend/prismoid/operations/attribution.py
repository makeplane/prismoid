# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The audit envelope's one constructor (ADR-062): every human action names its actor."""

from prismoid.db.models.actor import Actor
from prismoid.schemas.events import ActorRef
from prismoid.schemas.ids import ActorId


def actor_ref(actor: Actor) -> ActorRef:
    """The one place an external payload says "actor"."""
    kind = "user" if actor.is_human() else "agent"
    return ActorRef(type=kind, id=ActorId(actor.id), name=actor.display_name)
