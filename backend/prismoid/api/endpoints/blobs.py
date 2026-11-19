# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The blob door - local mode's replacement for an S3 presigned URL (ADR-077).

The one route that serves bytes with no Actor: its HMAC signature, checked in `operations.blobs`,
is the whole of its auth (CLAUDE.md, Auth). It 404s unless the storage backend is `fs`. Thin
(Rule 3): validate through the operation, then stream the file. A plain `def`: FastAPI runs it in
its threadpool, so the disk checks never block the event loop.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query
from fastapi.responses import FileResponse

from prismoid.api.serializers.blobs import BlobQuery
from prismoid.operations import blobs as ops


def get_blob(digest: str, q: Annotated[BlobQuery, Query()]) -> FileResponse:
    blob = ops.open_blob(
        digest, bucket=q.bucket, name=q.name, inline=q.inline, media=q.media, exp=q.exp, sig=q.sig
    )
    return FileResponse(blob.path, media_type=blob.media, headers=blob.headers)
