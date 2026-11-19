# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Shapes shared by every resource: the page envelope, and the bare acknowledgement.

Added deliberately. `PageOut[T]` is not a
resource, it is the envelope every list endpoint returns, and one generic beats ten copies. `OkOut`
is the same idea for the smallest write: a typed `{"ok": true}` (no bare dict crosses the
boundary), shared because "it worked" is not a per-resource concept.
"""

from __future__ import annotations

from pydantic import BaseModel


class PageOut[T](BaseModel):
    """Keyset pagination, and only keyset. `next_cursor` is null on the last page.

    Every list endpoint is audited for this shape: a list route without a cursor is a route that
    silently truncates, and silently truncating is worse than paginating badly.
    """

    items: list[T]
    next_cursor: str | None = None


class OkOut(BaseModel):
    """The minimal typed acknowledgement - a write with nothing to return but its success."""

    ok: bool = True
