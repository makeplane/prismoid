# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What harvest stores for each published file: a collection is repaired when it can be (the bytes
change, the repairs are noted), kept as published with an honest note when it cannot, and any
other kind is stored untouched. Pure - no storage, no database."""

from __future__ import annotations

import json

import orjson
import pytest

from prismoid.db.base import utcnow
from prismoid.operations.artifacts import normalize_for_harvest
from prismoid.schemas.artifacts import ManifestEntry
from prismoid.schemas.collection_repair import repair_collection
from prismoid.schemas.outputs import ArtifactType
from tests.support.samples import FIXTURE, MUTATIONS


def _entry(kind: ArtifactType) -> ManifestEntry:
    return ManifestEntry(
        path="out/thing", type=kind, title="t", sha256="0" * 64, bytes=1, agent="a", model="m", at=utcnow()
    )


def test_a_repairable_collection_is_stored_repaired_with_its_notes() -> None:
    raw = json.dumps(MUTATIONS["a bad date"]).encode()
    data, repairs = normalize_for_harvest(_entry(ArtifactType.COLLECTION), raw)
    fixed = repair_collection(raw)
    assert repairs == fixed.repairs and repairs
    assert data == orjson.dumps(fixed.content, option=orjson.OPT_INDENT_2)


def test_an_unrepairable_collection_is_stored_as_published_with_the_note() -> None:
    raw = b'{"collection_version": 1, "title": "x", "fie'
    data, repairs = normalize_for_harvest(_entry(ArtifactType.COLLECTION), raw)
    assert data == raw
    assert repairs == repair_collection(raw).repairs and repairs


def test_a_valid_collection_is_stored_untouched() -> None:
    raw = json.dumps(FIXTURE).encode()
    assert normalize_for_harvest(_entry(ArtifactType.COLLECTION), raw) == (raw, [])


@pytest.mark.parametrize("kind", [k for k in ArtifactType if k is not ArtifactType.COLLECTION])
def test_every_other_kind_is_never_repaired(kind: ArtifactType) -> None:
    raw = b'{"collection_version": 1, "title": "x", "fie'
    assert normalize_for_harvest(_entry(kind), raw) == (raw, [])
