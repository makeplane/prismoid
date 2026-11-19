# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Content-addressed object storage (MinIO/S3, or the local disk). One module, used by `operations`
and by `ai/frames`.

It lives at the root, beside `errors.py` and `conf.py`, for the same reason: **both the engine and
the composer need it, and the engine may not import the composer**.
`operations/machines/store.py` keeps Machine checkpoints here, by explicit key; `operations`
harvests artifacts here. A module either of them could not reach would force one of them to grow a
second copy.

**Content-addressed, and that is not a detail.** An artifact is stored under the SHA-256 of its own
bytes. Three consequences, all load-bearing:

* the same content published twice costs one object;
* a download can never disagree with the verification that signed it, because the key *is* the
  hash the report attests to;
* a tampered object is a *missing* object - you cannot quietly swap bytes under a key that is
  derived from them.

`settings.worker.storage_backend` picks the store: `s3` (below) or `fs` (`prismoid/storage_fs.py`, where a
"presigned URL" is a link to the signed blob door). The functions and their signatures are the same.

Every call blocks (boto3, or the disk), so async code calls the `a`-prefixed twin at the bottom of
this file, which runs the same function on a worker thread: a blocking call on the event loop stalls
every task the worker is running.

Two endpoints on purpose: the worker **uploads** over the internal network; a browser
**downloads** a presigned URL over the public one. They are identical in dev and must not be in
production - collapsing them now would silently mint presigned links pointing at a hostname no
client can resolve.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import re
from functools import lru_cache
from typing import IO, Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from prismoid import storage_fs
from prismoid.conf import settings


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _region(endpoint: str) -> str:
    """The region a request is signed for: AWS S3 names it in the endpoint, which must agree;
    anything else (MinIO and other S3 stores) takes us-east-1."""
    found = re.search(r"//s3[.-]([a-z0-9-]+)\.amazonaws\.com", endpoint)
    return found.group(1) if found else "us-east-1"


@lru_cache(maxsize=2)
def _client(endpoint: str) -> Any:
    # No key pair (S3 reached with the pod's IAM role, IRSA): boto3's default credential chain.
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.worker.s3_access_key or None,
        aws_secret_access_key=settings.worker.s3_secret_key.get_secret_value() or None,
        config=Config(signature_version="s3v4"),
        region_name=_region(endpoint),
    )


def _internal() -> Any:
    """Where the worker uploads."""
    return _client(settings.worker.s3_endpoint)


def _public() -> Any:
    """Where a browser downloads. A presigned URL is signed *for this host*."""
    return _client(settings.s3_public_endpoint)


def _fs() -> bool:
    """The backend is chosen by config, read per call so a test can flip it."""
    return settings.worker.storage_backend == "fs"


def key_for(digest: str) -> str:
    """`ab/cd/abcdef…` - sharded two levels, because a bucket with a million keys in one prefix is
    a bucket that lists slowly forever."""
    return f"{digest[:2]}/{digest[2:4]}/{digest}"


def put(data: bytes, bucket: str | None = None) -> str:
    """Store bytes under their own hash. Idempotent by construction - the same bytes are the same
    key, so a re-publish is a no-op rather than a duplicate."""
    bucket = bucket or settings.worker.artifact_bucket
    digest = sha256(data)
    key = key_for(digest)
    if _fs():
        storage_fs.put_bytes(data, bucket, key)
        return digest
    _internal().put_object(Bucket=bucket, Key=key, Body=data)
    return digest


def get(digest: str, bucket: str | None = None) -> bytes:
    bucket = bucket or settings.worker.artifact_bucket
    if _fs():
        return storage_fs.get_bytes(bucket, key_for(digest))
    body = _internal().get_object(Bucket=bucket, Key=key_for(digest))["Body"]
    return bytes(body.read())


def exists(digest: str, bucket: str | None = None) -> bool:
    bucket = bucket or settings.worker.artifact_bucket
    if _fs():
        return storage_fs.exists(bucket, key_for(digest))
    try:
        _internal().head_object(Bucket=bucket, Key=key_for(digest))
    except ClientError:
        return False
    return True


def delete(digest: str, bucket: str | None = None) -> None:
    """Remove a content-addressed object. **Idempotent** - a missing key is success, because the
    only correct time to call this is on a blob nothing references, and "gone" is the goal.

    The caller owns the dedup safety: a digest is shared across everything with the same bytes
    (an artifact published twice, or a blob that is one task's artifact and another's input), so
    a blob must be proven unreferenced before it is deleted here. A leaked blob is a storage
    cost; a wrongly-deleted one is data loss for a task that still points at it - the asymmetry
    is why this method never guesses and the caller always checks first.
    """
    bucket = bucket or settings.worker.artifact_bucket
    if _fs():
        return storage_fs.delete_key(key_for(digest), bucket)
    # already gone, or a transient store error - never worth failing a delete over.
    with contextlib.suppress(ClientError):
        _internal().delete_object(Bucket=bucket, Key=key_for(digest))


def presign(
    digest: str,
    filename: str,
    bucket: str | None = None,
    expires: int = 3600,
    *,
    inline: bool = False,
    media: str | None = None,
) -> str:
    """A time-limited link, signed for the **public** endpoint.

    `inline` + `media` are the display half: a rendition shown INSIDE the reader
    must say `Content-Disposition: inline` and its real content type, or the browser treats
    it as a download and an embedded PDF renders as nothing - found live: sheets displayed
    (their HTML is fetch()ed, which ignores disposition) while every PDF stayed blank.
    Downloads keep `attachment`, exactly as before."""
    bucket = bucket or settings.worker.artifact_bucket
    if _fs():
        return storage_fs.presign(
            digest, key_for(digest), filename, bucket, expires, inline=inline, media=media
        )
    disposition = "inline" if inline else "attachment"
    params: dict[str, str] = {
        "Bucket": bucket,
        "Key": key_for(digest),
        "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
    }
    if media is not None:
        params["ResponseContentType"] = media
    return str(_public().generate_presigned_url("get_object", Params=params, ExpiresIn=expires))


# ── by explicit key: Machine checkpoints, NOT content-addressed ───────────────────────────────


Missing = storage_fs.Missing  # an object that is not there (S3 404 / NoSuchKey, or no such file)


def _missing(err: ClientError) -> bool:
    return str(err.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}


def put_file(key: str, fileobj: IO[bytes], bucket: str | None = None) -> None:
    """Upload by explicit key, multipart past boto3's threshold - memory stays bounded."""
    if _fs():
        return storage_fs.put_file(key, fileobj, bucket or settings.worker.artifact_bucket)
    _internal().upload_fileobj(fileobj, bucket or settings.worker.artifact_bucket, key)


def get_file(key: str, fileobj: IO[bytes], bucket: str | None = None) -> None:
    if _fs():
        return storage_fs.get_file(key, fileobj, bucket or settings.worker.artifact_bucket)
    try:
        _internal().download_fileobj(bucket or settings.worker.artifact_bucket, key, fileobj)
    except ClientError as err:
        if _missing(err):
            raise Missing(key) from err
        raise


def copy_key(src: str, dst: str, bucket: str | None = None) -> None:
    """A server-side copy (managed: multipart for large objects) - nothing crosses the worker."""
    name = bucket or settings.worker.artifact_bucket
    if _fs():
        return storage_fs.copy_key(src, dst, name)
    try:
        _internal().copy({"Bucket": name, "Key": src}, name, dst)
    except ClientError as err:
        if _missing(err):
            raise Missing(src) from err
        raise


def delete_key(key: str, bucket: str | None = None) -> None:
    if _fs():
        return storage_fs.delete_key(key, bucket or settings.worker.artifact_bucket)
    with contextlib.suppress(ClientError):
        _internal().delete_object(Bucket=bucket or settings.worker.artifact_bucket, Key=key)


def list_keys(prefix: str, bucket: str | None = None) -> list[str]:
    if _fs():
        return [key for key, _ in storage_fs.list_objects(prefix, bucket or settings.worker.artifact_bucket)]
    paginator = _internal().get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket or settings.worker.artifact_bucket, Prefix=prefix):
        keys.extend(str(item["Key"]) for item in page.get("Contents", []))
    return keys


def last_modified(key: str, bucket: str | None = None) -> dt.datetime | None:
    """When `key` was last written (a HEAD), or None when there is no such object."""
    if _fs():
        return storage_fs.last_modified(key, bucket or settings.worker.artifact_bucket)
    try:
        head = _internal().head_object(Bucket=bucket or settings.worker.artifact_bucket, Key=key)
    except ClientError as err:
        if _missing(err):
            return None
        raise
    return head["LastModified"]


def list_objects(prefix: str, bucket: str | None = None) -> list[tuple[str, dt.datetime]]:
    """Every key under `prefix`, with when it was last written - the orphan arm's listing."""
    if _fs():
        return storage_fs.list_objects(prefix, bucket or settings.worker.artifact_bucket)
    paginator = _internal().get_paginator("list_objects_v2")
    out: list[tuple[str, dt.datetime]] = []
    for page in paginator.paginate(Bucket=bucket or settings.worker.artifact_bucket, Prefix=prefix):
        out.extend((str(item["Key"]), item["LastModified"]) for item in page.get("Contents", []))
    return out


def delete_prefix(prefix: str, bucket: str | None = None) -> int:
    name = bucket or settings.worker.artifact_bucket
    if _fs():
        return storage_fs.delete_prefix(prefix, name)
    keys = list_keys(prefix, name)
    for start in range(0, len(keys), 1000):
        batch = [{"Key": key} for key in keys[start : start + 1000]]
        _internal().delete_objects(Bucket=name, Delete={"Objects": batch, "Quiet": True})
    return len(keys)


# ── the same calls off the event loop, for async code ─────────────────────────────────────────


async def aput(data: bytes, bucket: str | None = None) -> str:
    return await asyncio.to_thread(put, data, bucket)


async def aget(digest: str, bucket: str | None = None) -> bytes:
    return await asyncio.to_thread(get, digest, bucket)


async def adelete(digest: str, bucket: str | None = None) -> None:
    await asyncio.to_thread(delete, digest, bucket)


async def aput_file(key: str, fileobj: IO[bytes], bucket: str | None = None) -> None:
    await asyncio.to_thread(put_file, key, fileobj, bucket)


async def aget_file(key: str, fileobj: IO[bytes], bucket: str | None = None) -> None:
    await asyncio.to_thread(get_file, key, fileobj, bucket)


async def acopy_key(src: str, dst: str, bucket: str | None = None) -> None:
    await asyncio.to_thread(copy_key, src, dst, bucket)


async def adelete_key(key: str, bucket: str | None = None) -> None:
    await asyncio.to_thread(delete_key, key, bucket)


async def alist_keys(prefix: str, bucket: str | None = None) -> list[str]:
    return await asyncio.to_thread(list_keys, prefix, bucket)


async def alist_objects(prefix: str, bucket: str | None = None) -> list[tuple[str, dt.datetime]]:
    return await asyncio.to_thread(list_objects, prefix, bucket)


async def alast_modified(key: str, bucket: str | None = None) -> dt.datetime | None:
    return await asyncio.to_thread(last_modified, key, bucket)


async def adelete_prefix(prefix: str, bucket: str | None = None) -> int:
    return await asyncio.to_thread(delete_prefix, prefix, bucket)
