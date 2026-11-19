# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The unit-of-work tenant binding: bind, platform mode, fail closed."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import tenant
from prismoid.errors import TenantUnbound


def _session() -> AsyncSession:
    return AsyncSession(bind=None)  # never connects; only .info is used


def test_unbound_session_fails_closed() -> None:
    with pytest.raises(TenantUnbound):
        tenant.workspace_of(_session())


def test_bind_then_read() -> None:
    s, workspace = _session(), uuid.uuid4()
    tenant.bind(s, workspace, "admin")
    assert tenant.workspace_of(s) == workspace
    assert tenant.role_of(s) == "admin"
    assert tenant.bound_workspace(s) == workspace
    assert not tenant.is_platform(s)


def test_platform_mode_refuses_workspace_of() -> None:
    s = _session()
    tenant.bind_platform(s)
    assert tenant.is_platform(s)
    assert tenant.bound_workspace(s) is None
    with pytest.raises(TenantUnbound):
        tenant.workspace_of(s)


def test_rebinding_replaces_platform_mode() -> None:
    s, workspace = _session(), uuid.uuid4()
    tenant.bind_platform(s)
    tenant.bind(s, workspace)
    assert tenant.workspace_of(s) == workspace and not tenant.is_platform(s)
