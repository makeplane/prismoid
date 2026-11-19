# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step's `ask_user` batch as the gate opens it: the mid-run set, built now."""

from __future__ import annotations

from collections.abc import Sequence

from prismoid.db.base import utcnow
from prismoid.operations.questions import mid_run
from prismoid.schemas.plan import QuestionSet


def question_set(*batch: tuple[str, Sequence[str]]) -> QuestionSet:
    """`(prompt, options)` pairs as one mid-run question set that waits for a person."""
    return mid_run(batch, now=utcnow())
