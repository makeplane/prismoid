# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The filesystem object store (ADR-077): `prismoid.storage` on one Mac, with no MinIO and no S3.

`prismoid.storage` is the public face and picks this backend when `settings.worker.storage_backend`
is `fs`; nothing else imports this module except the blob door (`operations/blobs.py`). Objects
live at `<home>/objects/<bucket>/<key>` with the same content-addressed keys as S3, so a bucket
directory is a bucket and a key is a relative path.

**Every key is checked, not trusted.** A key is a relative path of plain segments (no `..`, no
`.`, no empty segment, no leading `/`), and the resolved real path must still sit inside the
bucket's real root - so a symlink planted under the bucket cannot carry a read or a write out.

**Writes are atomic.** A temp file in the destination's own directory, then `os.replace`: a
reader sees the old object or the whole new one, never half of one, and a crash leaves a
`.tmp.*` file that listings skip.

**The blob door's signature lives here too**, beside the one function (`presign`) that mints it:
S3 presigns for a browser; the filesystem has no such thing, so Prismoid signs its own link
(HMAC-SHA256 over a canonical encoding of everything the door will honour) and the API serves it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import IO
from urllib.parse import urlencode

from prismoid import keyfile
from prismoid.conf import settings

# The longest a link may live: S3 SigV4's own ceiling (7 days), which `presign` never enforced in
# code - its callers all ask for the default hour. The door applies it to `exp` as well, so even a
# leaked key could not mint a link that outlives what S3 could have.
MAX_TTL_S = 7 * 24 * 3600
KEY_FILE = "blob.key"
_TMP_PREFIX = ".tmp."


class Missing(Exception):
    """An object that is not there. `prismoid.storage.Missing` IS this class."""


class BlobKeyError(keyfile.KeyFileError):
    """The blob key is unusable; the door refuses to sign or verify rather than open itself."""


# ── layout ────────────────────────────────────────────────────────────────────────────────────


def buckets() -> frozenset[str]:
    """The buckets the blob door will serve: the ones `presign` is called with. Every caller links
    an artifact (or a rendition of one); checkpoints and skill bodies are never given to a browser."""
    return frozenset({settings.worker.artifact_bucket})


def _root(bucket: str) -> Path:
    if not bucket or bucket in (".", "..") or any(c in bucket for c in "/\\\0"):
        raise ValueError(f"not a bucket name: {bucket!r}")
    return settings.local.home / "objects" / bucket


def _inside(root: Path, path: Path) -> Path:
    real_root = Path(os.path.realpath(root))
    real = Path(os.path.realpath(path))
    if real != real_root and real_root not in real.parents:
        raise ValueError("path leaves its bucket")
    return real


def _checked(bucket: str, key: str, *, allow_empty: bool = False) -> Path:
    """The REAL path of `key` in `bucket`, proved to be inside the bucket's real root - the checked
    path is the one used, so a symlink swapped in after the check cannot redirect the access."""
    if key or not allow_empty:
        parts = key.split("/")
        if not key or key.startswith("/") or "\0" in key or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"not a storage key: {key!r}")
    root = _root(bucket)
    return _inside(root, root / key if key else root)


def object_path(bucket: str, key: str) -> Path:
    """Where `key` lives, for the blob door - checked like every other access."""
    return _checked(bucket, key)


def _tmp_beside(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.parent / f"{_TMP_PREFIX}{os.getpid()}.{secrets.token_hex(4)}"


# ── the by-digest API ─────────────────────────────────────────────────────────────────────────


def put_bytes(data: bytes, bucket: str, key: str) -> None:
    path = _checked(bucket, key)
    if path.is_file() and path.stat().st_size == len(data):
        return  # content-addressed and the same size: already there (a rewrite is a no-op)
    tmp = _tmp_beside(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def get_bytes(bucket: str, key: str) -> bytes:
    try:
        return _checked(bucket, key).read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as err:
        raise Missing(key) from err


def exists(bucket: str, key: str) -> bool:
    return _checked(bucket, key).is_file()


# ── by explicit key (Machine checkpoints) ─────────────────────────────────────────────────────


def put_file(key: str, fileobj: IO[bytes], bucket: str) -> None:
    path = _checked(bucket, key)
    tmp = _tmp_beside(path)
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(fileobj, out)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def get_file(key: str, fileobj: IO[bytes], bucket: str) -> None:
    try:
        with _checked(bucket, key).open("rb") as src:
            shutil.copyfileobj(src, fileobj)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as err:
        raise Missing(key) from err


def copy_key(src: str, dst: str, bucket: str) -> None:
    source, target = _checked(bucket, src), _checked(bucket, dst)
    if not source.is_file():
        raise Missing(src)
    tmp = _tmp_beside(target)
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def delete_key(key: str, bucket: str) -> None:
    with contextlib.suppress(OSError):  # already gone is the goal
        _checked(bucket, key).unlink()


def list_objects(prefix: str, bucket: str) -> list[tuple[str, dt.datetime]]:
    """S3's `list_objects_v2` as callers use it: every key that STARTS WITH `prefix` (a string
    prefix, not a directory), sorted by key, with its last-written time (aware, UTC)."""
    directory = prefix.rpartition("/")[0]
    root = Path(os.path.realpath(_root(bucket)))
    start = _checked(bucket, directory, allow_empty=True)
    out: list[tuple[str, dt.datetime]] = []
    for here, dirs, files in os.walk(start):  # followlinks=False: a symlinked dir is never entered
        dirs.sort()
        for name in files:
            if name.startswith(_TMP_PREFIX):
                continue
            path = Path(here) / name
            key = path.relative_to(root).as_posix()
            if key.startswith(prefix) and path.is_file() and not path.is_symlink():
                out.append((key, dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)))
    out.sort(key=lambda item: item[0])
    return out


def last_modified(key: str, bucket: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(_checked(bucket, key).stat().st_mtime, dt.UTC)
    except (FileNotFoundError, NotADirectoryError):
        return None


def delete_prefix(prefix: str, bucket: str) -> int:
    keys = [key for key, _ in list_objects(prefix, bucket)]
    for key in keys:
        delete_key(key, bucket)
    return len(keys)


# ── the blob door's signature ─────────────────────────────────────────────────────────────────


def blob_key() -> bytes:
    """The HMAC key: 32 random bytes in `<home>/blob.key`, mode 0600 (`prismoid.keyfile`: atomic
    creation, refuses an empty, loose, foreign or symlinked file)."""
    try:
        key = keyfile.ensure(
            settings.local.home / KEY_FILE,
            lambda: secrets.token_bytes(32).hex(),
            restart="start Prismoid again",
        )
    except keyfile.KeyFileError as err:
        raise BlobKeyError(str(err)) from err
    return key.encode()


def _message(digest: str, bucket: str, name: str, inline: bool, media: str, *, exp: int) -> bytes:
    """What is signed: a JSON array, so every field has a boundary (concatenation lets
    `name="xy", media=""` collide with `name="x", media="y"`)."""
    return json.dumps([digest, bucket, name, inline, media, exp], separators=(",", ":")).encode()


def sign(digest: str, bucket: str, name: str, inline: bool, media: str, *, exp: int) -> str:
    return hmac.new(
        blob_key(), _message(digest, bucket, name, inline, media, exp=exp), hashlib.sha256
    ).hexdigest()


def verify(
    digest: str,
    bucket: str,
    name: str,
    inline: bool,
    media: str,
    *,
    exp: int,
    sig: str,
    now: float | None = None,
) -> bool:
    """True iff `sig` signs exactly these fields and `exp` is in the future and within the cap."""
    current = time.time() if now is None else now
    if exp <= current or exp > current + MAX_TTL_S:
        return False
    if not sig.isascii():
        return False  # compare_digest raises on non-ASCII str; that is a bad signature, not an error
    return hmac.compare_digest(sign(digest, bucket, name, inline, media, exp=exp), sig)


def presign(
    digest: str, key: str, filename: str, bucket: str, expires: int, *, inline: bool, media: str | None
) -> str:
    """The door's URL on the API's public address. `key` is accepted for symmetry with S3 but the
    door derives it from `digest`: only content-addressed objects are ever linked."""
    exp = int(time.time()) + max(1, min(expires, MAX_TTL_S))
    query = {"bucket": bucket, "name": filename, "inline": "1" if inline else "0"}
    if media:
        query["media"] = media
    query["exp"] = str(exp)
    query["sig"] = sign(digest, bucket, filename, inline, media or "", exp=exp)
    return f"{settings.api.public_base_url}/v1/blobs/{digest}?{urlencode(query)}"
