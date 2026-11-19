# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A bot's proposal carries a document's file format typed: a format alone implies a document, and beside
another kind it is dropped so a grant-start never 422s on it (the task door's rule)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prismoid.schemas.conversation import TaskProposal


def test_a_format_alone_implies_a_document() -> None:
    card = TaskProposal(goal="the summary", format="docx")
    assert (card.deliverable, card.format) == ("document", "docx")


def test_a_format_beside_another_kind_is_dropped() -> None:
    card = TaskProposal(goal="the pipeline", deliverable="spreadsheet", format="docx")
    assert (card.deliverable, card.format) == ("spreadsheet", None)


def test_a_stored_card_without_a_format_still_reads() -> None:
    card = TaskProposal.model_validate({"goal": "g", "deliverable": "document", "status": "started"})
    assert card.format is None


def test_only_a_known_format_rides() -> None:
    with pytest.raises(ValidationError):
        TaskProposal.model_validate({"goal": "g", "format": "exe"})
