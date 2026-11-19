# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid admin bootstrap` refuses an address the login door would refuse, before it touches the
database: an account nobody could ever sign in to is not an admin, it is a dead row."""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.errors import ValidationFailed
from prismoid.operations import admin_ops


class _Untouched:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"bootstrap touched the database ({name}) for an address login refuses")


@pytest.mark.parametrize("email", ["admin@live.test", "not-an-address", "admin@localhost", ""])
async def test_an_address_login_would_refuse_is_refused_before_any_row(email: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await admin_ops.bootstrap_admin(_Untouched(), email=email)  # pyright: ignore[reportArgumentType]
    assert "email" in refused.value.fields
