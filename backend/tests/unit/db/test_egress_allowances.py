# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The always-allow list is rows with provenance - and the same host vet the gate
uses guards the settings door."""

from __future__ import annotations

import pytest

from prismoid.api.serializers.egress_allowances import EgressAllowanceCreateIn
from prismoid.db.models import ALL_TABLES
from prismoid.db.models.egress_allowance import EgressAllowance


def test_the_table_is_registered() -> None:
    assert "egress_allowances" in ALL_TABLES
    assert EgressAllowance.__tablename__ == "egress_allowances"


def test_the_create_shape_takes_a_host_only() -> None:
    assert set(EgressAllowanceCreateIn.model_fields) == {"host"}
    with pytest.raises(ValueError):
        EgressAllowanceCreateIn(host="")
