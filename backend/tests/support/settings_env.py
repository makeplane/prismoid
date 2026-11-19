# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A process with no Prismoid configuration of its own, for a test that builds settings and asserts a default.

`Settings()` reads the real environment and then `.env`, a path relative to the working directory, so
a test run from `backend/` reads the developer's own `.env`, and anything the shell exported reaches it
too. A test about what a box does when nobody configured it must see neither.
"""

from __future__ import annotations

import os
import pathlib

import pytest

# The suite's own guard (tests/conftest.py): it keeps the developer's `~/.prismoid/config` out.
_KEPT = frozenset({"PRISMOID_CONFIG_FILE"})


def no_developer_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Drop every exported `PRISMOID_*` variable and move to a directory with no `.env`, for this test only."""
    for name in list(os.environ):
        if name.startswith("PRISMOID_") and name not in _KEPT:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
