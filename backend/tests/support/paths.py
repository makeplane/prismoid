# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The fixed folders a test reads from, so a test file's own depth never decides a path."""

from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
BACKEND = TESTS.parent
REPO = BACKEND.parent
