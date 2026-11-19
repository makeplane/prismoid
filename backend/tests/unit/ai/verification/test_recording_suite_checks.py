# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The recording suite's checks, one decision each: where the project root is, what counts as
an action row, which pictures are missing, whether the count matches, and whether a row
carries a typed value. Pure: tars built in memory."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Mapping, Sequence

import pytest

from prismoid.ai.verification.suites.recording import check_recording

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _row(file: str, **extra: object) -> dict[str, object]:
    return {"verb": "click", "target": "", "host": "h", "path": "/", "at": "t", "file": file, **extra}


def _tar(actions: Sequence[Mapping[str, object]], files: Mapping[str, bytes], prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        entries = {"index.html": b"<!doctype html>", "actions.json": json.dumps(actions).encode(), **files}
        for name, data in entries.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _by_name(checks: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(c["name"]): c for c in checks}


@pytest.mark.parametrize("prefix", ["", "recording-1/"])
def test_one_wrapping_directory_is_the_project_root(prefix: str) -> None:
    checks = _by_name(check_recording(_tar([_row("a.jpg")], {"a.jpg": JPEG}, prefix=prefix), 1))
    assert checks["format:tar"]["status"] == "pass"
    assert checks["format:tar"]["detail"] == "3 file(s), index.html and actions.json present"


def test_two_wrapping_directories_are_not_stripped() -> None:
    checks = check_recording(_tar([], {}, prefix="a/b/"), None)
    assert [(c["name"], c["status"]) for c in checks] == [("format:tar", "fail")]
    assert checks[0]["detail"] == "index.html and actions.json must sit at the root"


def test_an_actions_file_that_is_not_a_list_ends_the_checks() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in {"index.html": b"x", "actions.json": b'{"a": 1}'}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    checks = check_recording(buf.getvalue(), 0)
    assert [(c["name"], c["status"]) for c in checks] == [("format:tar", "pass"), ("format:actions", "fail")]
    assert checks[1]["detail"] == "actions.json is not a JSON list"


def test_the_checks_come_in_their_order() -> None:
    checks = check_recording(_tar([_row("a.jpg")], {"a.jpg": JPEG}), 1)
    assert [c["name"] for c in checks] == [
        "format:tar",
        "format:actions",
        "format:images",
        "format:count",
        "format:no-values",
    ]
    assert all(c["status"] == "pass" for c in checks)


def test_a_row_missing_keys_is_named_by_index() -> None:
    rows = [_row("a.jpg"), {"file": "a.jpg"}]
    action = _by_name(check_recording(_tar(rows, {"a.jpg": JPEG}), None))["format:actions"]
    assert (action["status"], action["detail"]) == ("fail", "2 action(s); rows missing keys: [1]")


@pytest.mark.parametrize(
    ("rows", "files", "status", "detail", "actual"),
    [
        ([_row("a.jpg"), _row("b.png")], {"a.jpg": JPEG, "b.png": PNG}, "pass", "2 image(s) present", None),
        (
            [_row("a.jpg"), _row("")],
            {"a.jpg": JPEG},
            "pass",
            "1 image(s) present; 1 capture(s) failed",
            None,
        ),
        (
            [_row("a.jpg"), _row("gone.jpg")],
            {"a.jpg": JPEG},
            "fail",
            "1 image(s) present",
            '{"absent": ["gone.jpg"], "not_image": []}',
        ),
        (
            [_row("a.jpg"), _row("t.jpg")],
            {"a.jpg": JPEG, "t.jpg": b"plain text"},
            "fail",
            "1 image(s) present",
            '{"absent": [], "not_image": ["t.jpg"]}',
        ),
    ],
)
def test_every_picture_is_present_and_an_image(
    rows: list[dict[str, object]], files: dict[str, bytes], status: str, detail: str, actual: str | None
) -> None:
    images = _by_name(check_recording(_tar(rows, files), None))["format:images"]
    assert (images["status"], images["detail"], images["actual"]) == (status, detail, actual)


@pytest.mark.parametrize(
    ("expected", "status", "detail"),
    [
        (None, "skip", "no journal count was supplied"),
        (1, "pass", "action count equals the journal's"),
        (2, "fail", "action count disagrees with the journal"),
    ],
)
def test_the_count_is_the_journals(expected: int | None, status: str, detail: str) -> None:
    count = _by_name(check_recording(_tar([_row("a.jpg")], {"a.jpg": JPEG}), expected))["format:count"]
    assert (count["status"], count["detail"]) == (status, detail)
    if expected is not None:
        assert (count["expected"], count["actual"]) == (str(expected), "1")


def test_a_row_carrying_a_typed_value_is_named_by_index() -> None:
    rows = [_row("a.jpg"), _row("a.jpg", value="hunter2")]
    leak = _by_name(check_recording(_tar(rows, {"a.jpg": JPEG}), None))["format:no-values"]
    assert (leak["status"], leak["detail"]) == ("fail", "rows carry values: [1]")
