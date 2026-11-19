# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Dispatch doubles: a test asserts wakes without a live DBOS. A plain module, so a test imports it
and never `tests.conftest` (the `recording_dispatcher`/`silent_dispatcher` fixtures install them)."""

from __future__ import annotations


class RecordingDispatcher:
    """Captures send/enqueue so tests assert wakes without a live DBOS."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, object, str]] = []
        self.enqueues: list[tuple[str, str]] = []
        self.arguments: dict[str, dict[str, object]] = {}  # by job key: what the job was started with
        self.delays: dict[str, float] = {}  # by job key: how long it was asked to wait

    async def enqueue(self, job: str, key: str, *, delay_s: float = 0.0, **kwargs: object) -> None:
        self.enqueues.append((job, key))
        self.arguments[key] = kwargs
        self.delays[key] = delay_s

    async def send(self, key: str, payload: object, topic: str, *, missing_ok: bool = False) -> None:
        self.sends.append((key, payload, topic))


class SilentDispatcher:
    """Swallows send/enqueue - for tests where dispatch is incidental, not the subject."""

    async def enqueue(self, job: str, key: str, **kwargs: object) -> None:
        return None

    async def send(self, key: str, payload: object, topic: str, *, missing_ok: bool = False) -> None:
        return None
