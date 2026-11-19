# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a tool can change: the side-effect class every tool in the tool catalog carries.

Not a durable effect (`@durable.effect` is the engine's word for a journaled step); this is the
vocabulary grants, steering's "acted outside" and approvals read.
"""

from __future__ import annotations

from enum import StrEnum


class SideEffect(StrEnum):
    """What a call can change, from nothing to the world outside Prismoid."""

    PURE = "pure"  # no I/O beyond the run
    READ = "read"  # reads the world or the task's files, changes nothing
    RECORD = "record"  # writes a Prismoid product record: an artifact, a proposal, a note
    LOCAL = "local"  # changes the task's own Machine only
    WORLD = "world"  # changes something outside Prismoid: a connector write, a browser act
    DEFERRAL = "deferral"  # parks the run on a person
