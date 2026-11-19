# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The contract version the API answers and the web client speaks are one number, 3 since the rename."""

from __future__ import annotations

import re

from prismoid.api.middleware import PROTOCOL_HEADER, PROTOCOL_VERSION
from tests.support.paths import REPO

CLIENT = REPO / "web" / "app" / "lib" / "api" / "client.ts"


def test_the_protocol_is_3_since_the_prismoid_rename() -> None:
    assert PROTOCOL_HEADER == "X-Prismoid-Protocol"
    assert PROTOCOL_VERSION == "3"


def test_the_web_client_speaks_the_same_version() -> None:
    found = re.search(r'export const API_PROTOCOL = "(\d+)";', CLIENT.read_text(encoding="utf-8"))
    assert found is not None, "web/app/lib/api/client.ts no longer declares API_PROTOCOL"
    assert found.group(1) == PROTOCOL_VERSION
