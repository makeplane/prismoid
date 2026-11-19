# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The store against real S3 (MinIO): multipart upload, server-side copy, prefix delete."""

import os
import uuid
from typing import Any

import pytest

from prismoid.operations.machines import store
from prismoid.schemas.machines import browser_key, checkpoint_key, machine_prefix, seed_key

pytestmark = pytest.mark.integration


async def test_a_large_checkpoint_survives_multipart_copy_and_prefix_delete(fake_frames: Any) -> None:
    mid, child = uuid.uuid4(), uuid.uuid4()
    frame = fake_frames("t")
    await frame.create()
    payload = os.urandom(12 * 1024 * 1024)  # past boto3's 8 MiB multipart threshold
    await frame.write_bytes("/work/blob.bin", payload)
    try:
        stored = await store.save_work(frame, checkpoint_key(mid, 1), cap=64 * 1024 * 1024)
        assert await store.list_prefix(machine_prefix(mid)) == [checkpoint_key(mid, 1)]
        await store.copy(stored.key, seed_key(child))
        restored = fake_frames("t2")
        await restored.create()
        await store.load_work(restored, seed_key(child), stored.sha256)
        assert await restored.read_bytes("/work/blob.bin") == payload
    finally:
        assert await store.delete_prefix(machine_prefix(mid)) == 1
        await store.delete_prefix(machine_prefix(child))
    assert await store.list_prefix(machine_prefix(mid)) == []


async def test_a_missing_object_is_lost_never_a_raw_s3_error(fake_frames: Any) -> None:
    gone = uuid.uuid4()
    frame = fake_frames("t")
    await frame.create()
    with pytest.raises(store.CheckpointLost):
        await store.load_work(frame, checkpoint_key(gone, 1), "0" * 64)
    with pytest.raises(store.CheckpointLost):
        await store.load_browser(frame, browser_key(gone, 1))
    with pytest.raises(store.CheckpointLost):
        await store.copy(checkpoint_key(gone, 1), seed_key(gone))
    assert await store.list_prefix(machine_prefix(gone)) == []
