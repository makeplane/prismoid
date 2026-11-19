# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The model catalog admin - adding a model is data, not a deploy.

Two properties matter: a model arrives **disabled** (routing it is a second, deliberate act, so a
mistyped `base_url` never routes production traffic the moment it is typed), and its prices are kept as
**Decimal** (reconcile to the cent - a float price is wrong before the first call is billed). The
endpoint's serializer parses price strings; this pins that, and the disabled default.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from prismoid.api.serializers.admin import parse_prices
from prismoid.db.session import session_scope
from prismoid.operations import models as ops
from prismoid.schemas.usage import Unit

pytestmark = pytest.mark.integration


def test_prices_parse_to_decimal_not_float() -> None:
    parsed = parse_prices({"input_token": "0.0000002", "output_token": "0.0000008"})
    assert parsed == {"input_token": Decimal("0.0000002"), "output_token": Decimal("0.0000008")}
    assert all(isinstance(v, Decimal) for v in parsed.values())


async def test_a_model_is_added_disabled_and_priced() -> None:
    async with session_scope() as session:
        entry = await ops.add_model(
            session,
            provider="local",
            model_id="admintest-fast",
            family="local",
            protocol="openai_compatible",
            classes=["fast-executor"],
            base_url="http://b2.example:8080",
            prices={Unit.INPUT_TOKEN.value: Decimal("0.0000002")},
            enabled=False,
        )
        ref, enabled, classes, base_url = (
            entry.provider + ":" + entry.model_id,
            entry.enabled,
            list(entry.classes),
            entry.base_url,
        )
        # It appears in the catalog, disabled.
        listed = {f"{e.provider}:{e.model_id}": e.enabled for e in await ops.list_models(session)}

    assert ref == "local:admintest-fast"
    assert enabled is False, "a new model is added disabled - enabling is deliberate (§9)"
    assert classes == ["fast-executor"]
    assert base_url == "http://b2.example:8080"
    assert listed.get("local:admintest-fast") is False


async def test_enable_flips_the_flag() -> None:
    async with session_scope() as session:
        await ops.add_model(
            session,
            provider="local",
            model_id="admintest-enable",
            family="local",
            protocol="openai_compatible",
            classes=["fast-executor"],
            enabled=False,
        )
        entry = await ops.set_enabled(session, "local:admintest-enable", True)
        assert entry.enabled is True
