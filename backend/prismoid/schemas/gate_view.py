# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What one viewer may read of a gate's reason. The task's creator reads it whole; everyone else
gets two cuts. A device-away gate's cut-off command is the creator's own command text
(`device_away.for_viewer`). A connector approval's arguments run on the creator's credential (an
email's recipients and body, a page's contents), so a teammate reads which connector and tool wait
on the creator, never what they would carry. Pure: the gates serializer and `events.redact_for`
both apply it where a gate leaves the server."""

from __future__ import annotations

from prismoid.schemas.device_away import for_viewer
from prismoid.schemas.plan import GateKind


def viewer_reason(reason: str, kind: GateKind, source: str) -> str:
    """A gate's reason as anyone but the task's creator reads it."""
    if kind is GateKind.APPROVE and source == "connector":
        # one block per held call, "connector: tool" then its arguments (`gate_open.open_approval`)
        return "\n\n".join(block.split("\n", 1)[0] for block in reason.split("\n\n"))
    return for_viewer(reason, creator=False)
