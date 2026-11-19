# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the DeviceFrame tests, shared by the
`test_device_driver*` files: the helper faked at the HTTP edge with `httpx.MockTransport`. A plain
module, not a test file."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.conf import settings
from prismoid.schemas.device import (
    FrameOut,
    FramePathsWire,
)
from prismoid.schemas.machines import LABEL_KIND

BASE = "/Users/me/Library/Application Support/Prismoid/tasks/t1"


PATHS = FramePathsWire(
    task=f"{BASE}/task",
    work="/Users/me/code/demo",
    artifacts=f"{BASE}/artifacts",
    skills=f"{BASE}/skills",
    verify=f"{BASE}/verify",
    inputs=f"{BASE}/task/inputs",
    browser="/Users/me/Library/Application Support/Prismoid/chrome",
    out=f"{BASE}/out",
)


FRAME = FrameOut(ref="device-t1", paths=PATHS, device_name="MacBook Pro")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Helper:
    """A fake helper: routes by (method, path suffix) and records every request."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any]], httpx.Response]] = {}

    def on(
        self, method: str, path: str, answer: Callable[[dict[str, Any]], httpx.Response] | httpx.Response
    ) -> None:
        self.routes[(method, path)] = answer if callable(answer) else (lambda _b, a=answer: a)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.sent.append((request.method, request.url.path, body))
        assert request.headers["X-Prismoid-Helper-Key"] == "k3y"
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"detail": "no such frame"})
        return route(body)

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [b for _m, p, b in self.sent if p == path]


@pytest.fixture(autouse=True)
def helper_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.local, "home", tmp_path)
    (tmp_path / "helper.key").write_text("k3y")


@pytest.fixture
def helper() -> Helper:
    h = Helper()
    h.on("POST", "/frames", httpx.Response(200, json=FRAME.model_dump(mode="json")))
    h.on("GET", "/frames/device-t1", httpx.Response(200, json=FRAME.model_dump(mode="json")))
    return h


def frame_for(helper: Helper, **kw: Any) -> DeviceFrame:
    return DeviceFrame("t1", transport=httpx.MockTransport(helper), **kw)


async def made(helper: Helper) -> DeviceFrame:
    frame = frame_for(helper)
    await frame.create(allow_domains=["a.example"], labels={LABEL_KIND: "machine"})
    return frame
