# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The filesystem's ctime clock, for tests that change a folder right after a walk recorded it.

The plants walk knows a folder is unchanged by its (device, inode, ctime). The kernel stamps ctime
from a coarse clock: on Linux one jiffy (1 to 4 ms), so a test that records a folder and changes it
within the same tick leaves the ctime as it was and the walk rightly skips the folder. The device
helper runs on macOS, whose APFS stamps carry nanoseconds; a fast Linux runner does not.
`wait_for_ctime_tick` returns once a change made now would carry a later ctime than any made before
the call: the condition itself, observed on a probe file, not a guess at how long a tick is.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

TICK_WAIT_S = 2.0  # far past any kernel's tick; reaching it means the clock is not moving


def wait_for_ctime_tick(folder: Path) -> None:
    """Return once the ctime clock of `folder`'s filesystem has moved past the moment of the call."""
    fd, name = tempfile.mkstemp(dir=folder, prefix=".ctime-probe-")
    os.close(fd)
    probe = Path(name)
    try:
        first = probe.stat().st_ctime_ns
        deadline = time.monotonic() + TICK_WAIT_S
        while True:
            os.utime(probe)  # a metadata change: the kernel stamps ctime with its clock's now
            if probe.stat().st_ctime_ns > first:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"the ctime clock under {folder} did not move in {TICK_WAIT_S}s")
    finally:
        probe.unlink()
