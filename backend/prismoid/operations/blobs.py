# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The blob door (ADR-077): the one place a signed link becomes a file on disk.

`GET /v1/blobs/{digest}` has no Actor - the HMAC signature in its query IS the auth (CLAUDE.md,
the Auth rule). So this module is the whole of that auth, and it is deliberately blunt:

* off unless this is local mode on the `fs` backend (a 404, as if the route did not exist);
* every failure before the file is found - no signature, a bad one, an expired or over-long one, a
  bucket `prismoid.storage` does not own, a digest that is not a SHA-256 - is the SAME 403, so the door
  never says which part was wrong and never says whether a blob exists to someone it did not sign for;
* only after the signature holds does a missing file become a 404;
* the file is the key the signature names, resolved inside its bucket by `storage_fs` (realpath
  containment: no `..`, no absolute path, no symlink out).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import logfire

from prismoid import storage, storage_fs
from prismoid.conf import settings
from prismoid.errors import Forbidden, NotFound, Unavailable

_DIGEST = re.compile(r"[0-9a-f]{64}")
_SIG = re.compile(r"[0-9a-f]{64}")
_EXP = re.compile(r"[0-9]{1,12}")
_MEDIA = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:; ?[\x20-\x7e]+)?")
_FALLBACK_MEDIA = "application/octet-stream"
_NAME_MAX = 200


@dataclass(frozen=True)
class Blob:
    path: Path
    media: str
    headers: dict[str, str]


def content_disposition(inline: bool, name: str) -> str:
    """RFC 6266: an ASCII-only quoted `filename` for old clients plus the real name as
    `filename*` (RFC 5987 percent-encoding). Control characters, quotes and path separators never
    survive into the header."""
    clean = "".join(c for c in name if c.isprintable())[:_NAME_MAX].replace("/", "_").replace("\\", "_")
    clean = clean.strip() or "download"
    ascii_name = "".join(c if c.isascii() and c not in '"%' else "_" for c in clean)
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean, safe='')}"


def open_blob(digest: str, *, bucket: str, name: str, inline: str, media: str, exp: str, sig: str) -> Blob:
    if settings.environment != "local" or settings.worker.storage_backend != "fs":
        raise NotFound("not found")
    flag = {"0": False, "1": True}.get(inline)
    try:
        valid = (
            flag is not None
            and _DIGEST.fullmatch(digest)
            and _SIG.fullmatch(sig)
            and _EXP.fullmatch(exp)
            and bucket in storage_fs.buckets()
            and storage_fs.verify(digest, bucket, name, flag, media, exp=int(exp), sig=sig)
        )
    except storage_fs.BlobKeyError:
        logfire.error(
            "blob door: the signing key file is unusable"
        )  # the message names a path: log, never serve
        raise Unavailable("the blob door is unavailable") from None
    if not valid or flag is None:
        raise Forbidden("this link is invalid or has expired")
    try:
        path = storage_fs.object_path(bucket, storage.key_for(digest))
    except ValueError:
        raise Forbidden("this link is invalid or has expired") from None
    if not path.is_file():
        raise NotFound("not found")
    kind = media if _MEDIA.fullmatch(media) else _FALLBACK_MEDIA
    headers = {
        "Content-Disposition": content_disposition(flag, name),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    # A browser's PDF viewer will not draw inside a sandboxed document, and a PDF's bytes run no
    # script in our origin; everything else - an inline HTML rendition above all - is sandboxed.
    if not (flag and kind.split(";")[0].strip().lower() == "application/pdf"):
        headers["Content-Security-Policy"] = "sandbox"
    return Blob(path=path, media=kind, headers=headers)
