# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A connector covers its own host and the hosts under it - never a sibling under a shared
public suffix, so a connector on `x.co.uk` does not hand the browser every `*.co.uk` site."""

from __future__ import annotations

from prismoid.operations.connectors import covered_by


def test_a_connector_covers_its_host_and_subdomains() -> None:
    assert covered_by({"x.co.uk"}, "x.co.uk")
    assert covered_by({"x.co.uk"}, "api.x.co.uk")


def test_a_connector_never_covers_a_sibling_under_the_public_suffix() -> None:
    assert not covered_by({"x.co.uk"}, "evil.co.uk")
    assert not covered_by({"mcp.linear.app"}, "evil.linear.app")
    assert not covered_by({"x.co.uk"}, "evilx.co.uk")
    assert not covered_by({""}, "anything.com")
