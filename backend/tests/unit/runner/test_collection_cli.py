# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`artifacts show`'s collection summary - a pure formatter, tested beside `_show`'s plan tree."""

from __future__ import annotations

from prismoid.runner.cli.artifacts import collection_summary

DOC: dict[str, object] = {
    "collection_version": 1,
    "title": "Q3 workstreams",
    "fields": [
        {"key": "name", "label": "Name", "type": "text"},
        {"key": "stage", "label": "Stage", "type": "select", "options": ["Todo", "Done"]},
        {"key": "due", "label": "Due", "type": "date"},
    ],
    "rows": [
        {"id": "r1", "cells": {"name": "Line expansion", "stage": "Todo", "due": "2026-08-01"}},
        {"id": "r2", "cells": {"name": "Field hiring"}},
    ],
    "views": [
        {"name": "Board", "layout": "board", "group_by": "stage"},
        {"name": "All", "layout": "table"},
    ],
}


def test_the_summary_names_fields_rows_and_views() -> None:
    text = collection_summary(DOC)
    assert "Name(text), Stage(select), Due(date)" in text
    assert "rows:    2" in text
    assert "Board (board), All (table)" in text


def test_the_head_table_shows_rows_and_dashes_sparse_cells() -> None:
    text = collection_summary(DOC)
    assert "Line expansion" in text
    assert "Field hiring" in text
    assert "-" in text, "a sparse cell renders as an em-dash, not an empty hole"


def test_many_rows_say_how_many_more() -> None:
    doc = dict(DOC)
    doc["rows"] = [{"id": f"r{i}", "cells": {"name": f"Item {i}"}} for i in range(9)]
    assert "… 4 more row(s)" in collection_summary(doc)
