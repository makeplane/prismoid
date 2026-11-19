# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The public auth config read (ADR-086): no Actor, the deployment and never an account."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from prismoid.conf import settings

pytestmark = pytest.mark.integration


async def test_the_config_is_public_and_says_open_by_default(client: AsyncClient) -> None:
    r = await client.get("/v1/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["signup"] == "open"


async def test_the_config_says_invite_when_signup_is_off(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.auth, "signup_enabled", False)
    assert (await client.get("/v1/auth/config")).json()["signup"] == "invite"


async def test_legal_and_support_links_come_from_settings(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (await client.get("/v1/auth/config")).json()
    assert (body["terms_url"], body["privacy_url"], body["support_email"]) == (None, None, None)
    monkeypatch.setattr(settings.api, "terms_url", "https://example.test/terms")
    monkeypatch.setattr(settings.api, "privacy_url", "https://example.test/privacy")
    monkeypatch.setattr(settings.api, "support_email", "help@example.test")
    body = (await client.get("/v1/auth/config")).json()
    assert body["terms_url"] == "https://example.test/terms"
    assert body["privacy_url"] == "https://example.test/privacy"
    assert body["support_email"] == "help@example.test"


async def test_signup_disabled_has_its_own_code(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.auth, "signup_enabled", False)
    r = await client.post(
        "/v1/auth/signup",
        json={"email": "nobody@example.com", "name": "No Body", "password": "a-long-enough-password"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "signup_disabled"
