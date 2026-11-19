# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The publish door's product half: where a published file lands and the manifest entry it gets.

The door is the PRODUCT's contract, not the sandbox's (`publish.py`), so this is its one Python
definition. A sandbox driver cannot call it: its publish runs inside the frame, as the static
`PUBLISH_RUNNER`, which writes these same fields. A field added here is added there. A driver
whose files live in this process (`paper.py`) builds its entries here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence

from prismoid.ai.frames.layout import ARTIFACTS
from prismoid.schemas.artifacts import ManifestEntry
from prismoid.schemas.outputs import ArtifactType


def artifact_target(path: str) -> str:
    """Where a file published from `path` lands: `/artifacts/<its basename>`."""
    return f"{ARTIFACTS}/{path.rsplit('/', 1)[-1]}"


def manifest_entry(
    target: str, artifact_type: ArtifactType, title: str, data: bytes, sources: Sequence[str]
) -> ManifestEntry:
    """The entry the runner appends for `data` published to `target`: hashed, sized, its sources
    named, stamped now. The agent, model and skill pins are filled at harvest, as for every driver."""
    return ManifestEntry(
        path=target,
        type=artifact_type,
        title=title,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        sources=list(sources),
        agent="",
        model="",
        skill_pins=[],
        at=dt.datetime.now(dt.UTC),
    )
