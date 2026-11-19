# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The API serves the built web app in local mode (ADR-077) without shadowing its own routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from prismoid.api.app import create_app
from prismoid.conf import settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def spa(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>prismoid spa</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path.parent / "secret.txt").write_text("outside")
    monkeypatch.setattr(settings.local, "web_dir", tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as c:
        yield c


async def test_spa_index_and_deep_link_fallback(spa: httpx.AsyncClient) -> None:
    root = await spa.get("/")
    deep = await spa.get("/projects/abc/tasks/1")
    asset = await spa.get("/assets/app.js")
    assert root.status_code == deep.status_code == asset.status_code == 200
    assert "prismoid spa" in root.text and "prismoid spa" in deep.text
    assert asset.text == "console.log(1)"
    assert all(r.headers["x-content-type-options"] == "nosniff" for r in (root, deep, asset))


async def test_v1_routes_not_shadowed_by_spa(spa: httpx.AsyncClient) -> None:
    health = await spa.get("/healthz")
    unknown = await spa.get("/v1/no/such/route")
    unknown_post = await spa.post("/v1/no/such/route")
    docs = await spa.get("/openapi.json")
    assert health.status_code == 200 and "prismoid spa" not in health.text
    assert unknown.status_code == unknown_post.status_code == 404
    assert unknown.headers["content-type"].startswith("application/json")
    assert "prismoid spa" not in unknown.text
    assert docs.status_code == 200 and docs.json()["info"]["title"] == "Prismoid"


async def test_spa_cannot_read_outside_web_dir(spa: httpx.AsyncClient) -> None:
    r = await spa.get("/..%2Fsecret.txt")
    assert "outside" not in r.text


async def test_no_spa_without_web_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.local, "web_dir", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as c:
        assert (await c.get("/")).status_code == 404
