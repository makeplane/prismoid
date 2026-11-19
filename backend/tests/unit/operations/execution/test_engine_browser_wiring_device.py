# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A recording made on the user's own computer: its copy lands in the
task's own `out/`, quoted, and is removed whether or not the publish went through - never in the
user's folder. The rig is `tests/support/rigs/browser_wiring_rig.py`."""

from __future__ import annotations

import os
from typing import Any

import pytest

from prismoid.operations.execution import harvest as ex_harvest
from tests.support.rigs.browser_wiring_rig import FakeFrame, facts


class _DeviceFrame(FakeFrame):
    """A frame on the user's own computer: its `/work` is their real
    folder, so it names a Prismoid-owned `staging_dir` - real, and under a home with a space in it."""

    def __init__(self, **kw: Any) -> None:
        from prismoid.ai.frames.layout import FramePaths

        super().__init__(**kw)
        home = "/Users/ada/Library/Application Support/Prismoid/tasks/t1"
        self.recording_dir = "/task/recordings/recording-1"
        self.paths = FramePaths(
            task=f"{home}/task", work="/Users/ada/code/demo", out=f"{home}/out", on_host=True
        )
        self.staging_dir = "/out"


@pytest.mark.parametrize("fail_publish", [False, True], ids=["published", "publish-fails"])
async def test_a_device_recording_is_never_staged_in_the_users_folder(fail_publish: bool) -> None:
    """Ruling a (D2 review): the copy lands in the task's own `out/`, quoted, and is removed
    whether or not the publish went through - nothing named `recording-pub-*` ever touches the
    work root, the user's repo."""
    import shlex

    from prismoid.ai.frames.layout import ARTIFACTS

    frame = _DeviceFrame(fail_publish=fail_publish)
    _, fact = facts()
    published = await ex_harvest.publish_recording(frame, object(), success=True, own_output=False, fact=fact)
    execs = [str(c[1]) for c in frame.calls if c[0] == "exec"]
    assert execs and not any("/work" in e or frame.paths.work in e for e in execs), execs
    copy, cleanup = execs[0], execs[-1]
    staged = next(c[1] for c in frame.calls if c[0] == "publish")
    assert staged.startswith("/out/recording-pub-1-"), "published by its virtual name"
    home = "/Users/ada/Library/Application Support/Prismoid/tasks/t1"
    real = shlex.quote(f"{home}{staged}")
    source = shlex.quote(f"{home}/task/recordings/recording-1")
    # Fix round 2: an earlier crash's copies are cleared first - this publish's has not begun.
    clear = f"find {shlex.quote(f'{home}/out')} -maxdepth 1 -name 'recording-pub-*' -exec rm -rf {{}} +"
    assert copy == f"{clear} ; cp -r {source} {real}"
    assert cleanup == f"rm -rf {real}", "removed even when the publish fails"
    expected = None if fail_publish else (f"{ARTIFACTS}/{os.path.basename(staged)}", "deliverable")
    assert published == expected
