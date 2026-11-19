# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a turn's plan asks of the task's Machine: the vocabulary `task.planned` wears. The rule that
decides it lives beside its one caller, `operations/machines/need.py`."""

from __future__ import annotations

from enum import StrEnum


class MachineNeed(StrEnum):
    """What a plan asks of the task's Machine, on `task.planned`."""

    NONE = "none"  # no step takes the task's Machine: the turn runs without one
    FILES = "files"  # its steps need only files: they run on paper, no Machine (a device task keeps its own)
    COMPUTER = "computer"  # a step's work runs on the computer: the Machine starts ahead of it
