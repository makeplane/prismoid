# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One switch for the contract suite: `--record-shapes` writes the shape each scenario produces
instead of comparing it, for the day a change of shape is meant. Pass the folder
(`pytest tests/contract --record-shapes`): pytest reads a folder's options only when it is named."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--record-shapes",
        action="store_true",
        default=False,
        help="write tests/contract/shapes/<scenario>.json from this run instead of comparing",
    )


@pytest.fixture
def record_shapes(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--record-shapes"))
