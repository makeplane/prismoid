# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One refusal table for both egress doors: `schemas/egress.refuse_ip` (here) and sandbar's Go
`refuseIP` (`internal/egress/proxy_test.go`) are tested against the same file, so the two
proxies refuse the same addresses - they once drifted on the documentation ranges, local-use
NAT64, Teredo and discard-only."""

from __future__ import annotations

import ipaddress
import json

import pytest

from prismoid.schemas.egress import refuse_ip
from tests.support.paths import REPO

_VECTORS = json.loads((REPO / "sandbar/internal/egress/testdata/refuse_ip.json").read_text())


@pytest.mark.parametrize("address", _VECTORS["refuse"])
def test_an_address_the_table_refuses_is_refused(address: str) -> None:
    assert refuse_ip(ipaddress.ip_address(address)) is not None


@pytest.mark.parametrize("address", _VECTORS["allow"])
def test_an_address_the_table_allows_passes(address: str) -> None:
    assert refuse_ip(ipaddress.ip_address(address)) is None
