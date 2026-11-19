# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Real-looking secrets for a test that boots `Settings` as staging or production: the boot refuses
the dev compose's well-known credentials there, so a test about another check sets these first."""

from __future__ import annotations

import pytest

REAL_SECRETS = {
    "PRISMOID_WORKER_S3_SECRET_KEY": "a-real-object-store-secret",
    "PRISMOID_API_DATABASE_URL": "postgresql+asyncpg://prismoid:a-real-password@db:5432/prismoid",
    "PRISMOID_WORKER_DATABASE_URL": "postgresql://prismoid:a-real-password@db:5432/prismoid",
    "PRISMOID_EGRESS_CONTROL_SECRET": "a-real-control-secret",
}


def use_real_secrets(monkeypatch: pytest.MonkeyPatch, **more: str) -> None:
    """Set the real-looking secrets, and any further `NAME=value` the test needs beside them."""
    for name, value in {**REAL_SECRETS, **more}.items():
        monkeypatch.setenv(name, value)
