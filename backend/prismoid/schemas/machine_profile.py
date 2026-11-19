# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The image a frame boots, and the one order the images keep.

The profiles are a line, narrowest first, each image layered on the one before it (`ai/frames/images.py`):
`base` (the office and data toolchain), `code` (plus git, ripgrep and node), `browser` (plus Chromium).
A Machine's profile only ever widens along it, because a wider image runs every narrower step.
"""

from __future__ import annotations

from enum import StrEnum


class MachineProfile(StrEnum):
    BASE = "base"
    CODE = "code"  # for a step whose format skill asks for it
    BROWSER = "browser"


def widen(current: str, needed: str) -> str:
    """The wider of two profiles: the image a Machine holding `current` needs to run a `needed` step."""
    return max(MachineProfile(current), MachineProfile(needed), key=list(MachineProfile).index)
