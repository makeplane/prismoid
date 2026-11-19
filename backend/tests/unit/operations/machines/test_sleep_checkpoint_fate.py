# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a finished sleep does with a Machine's stored objects. No I/O - the sleep's facts are
arguments."""

from __future__ import annotations

import pytest

from prismoid.operations.machines.sleep import CheckpointFate, checkpoint_fate


@pytest.mark.parametrize(
    ("facts", "fate"),
    [
        # over the cap everything goes, whatever else is true
        ({"dropped": True, "stored": False, "had_checkpoint": True}, CheckpointFate(True, "oversize")),
        # a fresh checkpoint replaces the old one; nothing is cleared
        ({"stored": True}, CheckpointFate(False)),
        # nothing new, but the last checkpoint still stands: the Machine sleeps on it
        ({"had_checkpoint": True}, CheckpointFate(False)),
        # a persistent frame is kept with no checkpoint at all
        ({"bare": True}, CheckpointFate(False)),
        # nothing stored, ever: the Machine is gone, its work lost
        ({}, CheckpointFate(True, "lost")),
        # a vanished frame's loss is said once, after the commit - not on the row
        ({"vanished": True}, CheckpointFate(True, None)),
    ],
)
def test_checkpoint_fate(facts: dict[str, bool], fate: CheckpointFate) -> None:
    args = {"dropped": False, "stored": False, "had_checkpoint": False, "bare": False, "vanished": False}
    args.update(facts)
    assert (
        checkpoint_fate(
            dropped=args["dropped"],
            stored=args["stored"],
            had_checkpoint=args["had_checkpoint"],
            bare=args["bare"],
            vanished=args["vanished"],
        )
        == fate
    )
