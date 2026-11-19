# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The closed vocabularies `prismoid` options take, as enums so `--help` lists them and a typo fails
before a round-trip. Each one mirrors the request field it fills (`api/serializers/tasks.py`,
`projects.py`, `gates.py`); `tests/unit/test_cli_choices.py` holds them equal, so neither drifts.

An option whose empty value meant "not given" defaults to `None` now, and the command sends the
field only when one was chosen, exactly as before.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """`--mode`: a standard plan, or every enabled model answering in parallel."""

    STANDARD = "standard"
    FLEET = "fleet"


class Clarify(StrEnum):
    """`--clarify`: whether the task may ask its one round of questions before it plans."""

    AUTO = "auto"
    NEVER = "never"
    ALWAYS = "always"


class Deliverable(StrEnum):
    """`--deliverable`: the artifact kind. `board` is how people say `collection`, the wire kind."""

    DOCUMENT = "document"
    BOARD = "board"
    COLLECTION = "collection"
    SPREADSHEET = "spreadsheet"
    DECK = "deck"
    CODE = "code"

    @property
    def wire(self) -> str:
        return "collection" if self is Deliverable.BOARD else self.value


class DocFormat(StrEnum):
    """`--format`: a document's file format."""

    MD = "md"
    DOCX = "docx"
    PDF = "pdf"
    HTML = "html"


class Size(StrEnum):
    """`--size`: `auto` (a literal number in the goal picks the bucket) or one of the five buckets."""

    AUTO = "auto"
    BRIEF = "brief"
    STANDARD = "standard"
    EXTENDED = "extended"
    LONG = "long"
    BOOK = "book"


class UsageBy(StrEnum):
    """`tasks usage --by`: break the cost down by model family or by person."""

    FAMILY = "family"
    PERSON = "person"


class MemberRole(StrEnum):
    """`projects share|invite-link --role`: what a member may do (the owner is a column, not a role)."""

    VIEWER = "viewer"
    EDITOR = "editor"


class ApproveScope(StrEnum):
    """`gates resolve --scope`: how far a browser approval reaches."""

    ONCE = "once"
    TASK = "task"
    ALWAYS = "always"
