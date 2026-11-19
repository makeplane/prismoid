# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The blob door's query (ADR-077). Every field is optional and a plain string on purpose: the
door answers 403 to anything it cannot verify, so a malformed `exp` must reach the signature check
rather than a 422 that tells a stranger which field they got wrong."""

from __future__ import annotations

from pydantic import BaseModel


class BlobQuery(BaseModel):
    bucket: str = ""
    name: str = ""
    inline: str = ""
    media: str = ""
    exp: str = ""
    sig: str = ""
