# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Shared pytest markers.

A device test proves something only on a Mac with Chrome, and CI has neither, so these skip. That is
right on a laptop and wrong when the point is the proof: `just device-proof` sets `PRISMOID_DEVICE_PROOF`,
and then a test that cannot run fails (the `must_run` mark, `tests/conftest.py`), so a green run means
each one ran here."""

import os
import sys
from typing import NoReturn

import pytest

from prismoid.conf import settings


def proving() -> bool:
    return os.environ.get("PRISMOID_DEVICE_PROOF") == "1"


def requires(met: bool, reason: str) -> pytest.MarkDecorator:
    """Skip unless `met`, or under the proof fail."""
    return (
        pytest.mark.must_run(reason) if proving() and not met else pytest.mark.skipif(not met, reason=reason)
    )


def cannot_run(reason: str) -> NoReturn:
    """`pytest.skip` from inside a test or a fixture, by the same rule."""
    if proving():
        pytest.fail(f"PRISMOID_DEVICE_PROOF: {reason}", pytrace=False)
    pytest.skip(reason)


darwin_only = requires(sys.platform == "darwin", "macOS only")
needs_chrome = requires(
    os.access(settings.local.chrome_path, os.X_OK), f"no Google Chrome at {settings.local.chrome_path}"
)
