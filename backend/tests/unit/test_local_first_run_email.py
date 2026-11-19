# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid up`'s first run refuses an owner email the web login would refuse (ADR-077).

The owner's address went straight to signup, while the login door validates an EmailStr: an owner
made as `me@prismoid.local` existed, held the CLI's key, and could never sign in to the web app. The
check runs before the lock and the seeds, so no session is needed to prove it."""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.errors import ValidationFailed
from prismoid.local import bootstrap

_PASSWORD = "a-long-enough-password"
_NO_SESSION = cast(AsyncSession, None)  # refused before the database is ever touched


@pytest.mark.parametrize(
    ("email", "why"),
    [
        ("me@prismoid.local", "special-use or reserved name"),
        ("not-an-address", "must have an @-sign"),
    ],
)
async def test_an_address_login_refuses_is_refused_at_first_run(email: str, why: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await bootstrap.first_run(_NO_SESSION, email=email, name="Me", password=_PASSWORD)
    assert why in refused.value.fields["email"]
    assert not refused.value.fields["email"].startswith("value is not")  # the reason, not pydantic's frame
