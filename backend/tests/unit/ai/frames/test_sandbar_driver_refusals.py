# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""sandbar's file door refusing a path (a 400, a 403 `forbidden_path`) is a policy refusal the
tools hand the model, never an `HTTPStatusError` or `FrameUnreachable` that fails the step."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from prismoid.ai.frames import sandbar_driver
from prismoid.ai.frames.sandbar_driver import SandbarFrame
from prismoid.errors import PathRefused, PolicyDenied

# ── sandbar's path refusals are policy refusals, never a dead step ──────────────────────────────


def _frame(monkeypatch: pytest.MonkeyPatch, refusal: Any) -> SandbarFrame:
    """A frame whose daemon answers every request with `refusal()`: the client is built over a mock
    transport, so no private attribute is touched."""
    real = httpx.AsyncClient

    def client(**kw: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(lambda request: refusal()), **kw)

    monkeypatch.setattr(sandbar_driver.httpx, "AsyncClient", client)
    return SandbarFrame(task_id="t1", profile="base")


def _forbidden() -> httpx.Response:
    return httpx.Response(403, json={"error": {"message": "forbidden", "code": "forbidden_path"}})


def _bad_path() -> httpx.Response:
    return httpx.Response(400, json={"error": {"message": "path must be clean and absolute"}})


@pytest.mark.parametrize("refusal", [_forbidden, _bad_path])
@pytest.mark.parametrize("op", ["read_bytes", "read_file", "list_dir", "write_bytes", "write_file"])
async def test_a_sandbar_path_refusal_is_a_path_refused(
    op: str, refusal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _frame(monkeypatch, refusal)
    args: tuple[Any, ...] = ("/browser/x",) if op.startswith(("read", "list")) else ("/browser/x", b"y")
    if op == "write_file":
        args = ("/browser/x", "y")
    with pytest.raises(PathRefused, match="/browser/x"):
        await getattr(frame, op)(*args)


async def test_sandbar_400_path_is_policy_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = _frame(monkeypatch, _bad_path)
    with pytest.raises(PolicyDenied, match=r"/work/\.\./x"):
        await frame.read_bytes("/work/../x")


async def test_a_403_with_another_code_is_not_a_path_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only `forbidden_path` is the path's refusal; any other 403 (a bad token) stays an error."""
    frame = _frame(
        monkeypatch, lambda: httpx.Response(403, json={"error": {"message": "no", "code": "forbidden"}})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await frame.read_bytes("/work/x")
