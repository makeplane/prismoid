# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every link in the product, built from `PUBLIC_BASE_URL`.

URL literals are forbidden outside conf/compose/env/providers - and this file, which is where the
*shapes* live. The reason is deployment: a hostname hard-coded in a serializer works perfectly on
localhost and hands the user a broken link in production.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.conf import settings


def _base() -> str:
    return settings.api.public_base_url.rstrip("/")


def _ws_base() -> str:
    """ws:// by scheme swap - one base URL, not two settings to disagree with each other."""
    base = _base()
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    return "ws://" + base[len("http://") :]


def task(task_id: UUID) -> str:
    return f"{_base()}/v1/tasks/{task_id}"


def task_stream(task_id: UUID) -> str:
    """The WebSocket. The SSE twin is `task_events()` - two framings of one generator."""
    return f"{_ws_base()}/v1/tasks/{task_id}/stream"


def task_events(task_id: UUID, since: int = 0) -> str:
    return f"{_base()}/v1/tasks/{task_id}/events?since={since}"


def artifact(artifact_id: UUID) -> str:
    return f"{_base()}/v1/artifacts/{artifact_id}"


def artifact_download(artifact_id: UUID) -> str:
    return f"{_base()}/v1/artifacts/{artifact_id}/download"


def gate(gate_id: UUID) -> str:
    return f"{_base()}/v1/gates/{gate_id}"
