# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/tests/unit/ai/verification/test_recording_suite.py
"""The recording verifies structurally and runs no model: images exist, hashes
line up with actions.json, and the action count equals what the journal saw."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Mapping, Sequence

from prismoid.ai.verification.suites.recording import check_recording
from prismoid.schemas.outputs import ARTIFACT_TYPES, ArtifactType, VerifierSuite, render_for
from prismoid.schemas.verification import Check

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _tar(
    actions: Sequence[Mapping[str, object]],
    files: dict[str, bytes],
    index: bool = True,
    index_name: str = "index.html",
    prefix: str = "",
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        if index:
            add(prefix + index_name, b"<!doctype html><title>Recording</title>")
        add(prefix + "actions.json", json.dumps(actions).encode())
        for name, data in files.items():
            add(prefix + name, data)
    return buf.getvalue()


ACTIONS = [
    {
        "seq": 1,
        "verb": "open",
        "target": "",
        "host": "127.0.0.1",
        "path": "/index.html",
        "at": "2026-09-28T10:00:00Z",
        "file": "0001-open.jpg",
    },
    {
        "seq": 2,
        "verb": "click",
        "target": 'button "Save"',
        "host": "127.0.0.1",
        "path": "/index.html",
        "at": "2026-09-28T10:00:01Z",
        "file": "0002-click.jpg",
    },
]


def _status(checks: list[dict[str, object]], name: str) -> str:
    return str(next(c["status"] for c in checks if c["name"] == name))


def test_the_type_is_registered_as_a_directory_with_its_own_suite() -> None:
    spec = ARTIFACT_TYPES[ArtifactType.RECORDING]
    assert spec.extensions == () and spec.verifier_suite is VerifierSuite.RECORDING
    assert render_for(ArtifactType.RECORDING, "recording-1.tar").value == "none"


def test_a_complete_recording_passes() -> None:
    checks = check_recording(
        _tar(ACTIONS, {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG}), expected_actions=2
    )
    assert all(c["status"] == "pass" for c in checks), checks


def test_a_missing_image_fails() -> None:
    checks = check_recording(_tar(ACTIONS, {"0001-open.jpg": JPEG}), expected_actions=2)
    assert _status(checks, "format:images") == "fail"


def test_a_failed_capture_placeholder_is_skipped_by_images_but_counted() -> None:
    """The daemon files `file: ""`, `capture: "failed"` when a frame capture fails after an
    action that happened: images skip it (and say so), the count still includes it."""
    rows = [
        {**ACTIONS[0]},
        {**ACTIONS[1], "file": "", "capture": "failed"},
    ]
    checks = check_recording(_tar(rows, {"0001-open.jpg": JPEG}), expected_actions=2)
    images = next(c for c in checks if c["name"] == "format:images")
    assert images["status"] == "pass" and "1 capture(s) failed" in str(images["detail"])
    assert _status(checks, "format:count") == "pass"
    assert _status(checks, "format:actions") == "pass"


def test_a_count_mismatch_fails_and_an_unknown_count_skips() -> None:
    tar = _tar(ACTIONS, {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG})
    assert _status(check_recording(tar, expected_actions=3), "format:count") == "fail"
    assert _status(check_recording(tar, expected_actions=None), "format:count") == "skip"


def test_every_check_parses_as_the_pipeline_reads_it() -> None:
    """The pipeline parses each check as a `Check`, whose `expected`/`actual` are text. Found
    live: the count's numbers and the image lists went out raw, every recording's receipt died
    in that parse, and its verdict stayed pending forever."""
    tar = _tar(ACTIONS, {"0001-open.jpg": JPEG})
    for checks in (
        check_recording(tar, expected_actions=3),  # the count disagrees: numbers on both sides
        check_recording(_tar(ACTIONS, {"0001-open.jpg": JPEG}, index=False), expected_actions=2),
    ):
        parsed = [Check.model_validate(c) for c in checks]
        assert all(c.expected is None or isinstance(c.expected, str) for c in parsed)
    count = next(c for c in check_recording(tar, expected_actions=3) if c["name"] == "format:count")
    assert (count["expected"], count["actual"]) == ("3", "2")
    images = next(c for c in check_recording(tar, expected_actions=2) if c["name"] == "format:images")
    assert json.loads(str(images["actual"])) == {"absent": ["0002-click.jpg"], "not_image": []}


def test_typed_values_in_an_action_row_fail() -> None:
    leaky = [dict(ACTIONS[0]), {**ACTIONS[1], "text": "hunter2"}]
    checks = check_recording(_tar(leaky, {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG}), expected_actions=2)
    assert _status(checks, "format:no-values") == "fail"


def test_no_index_fails_the_tar_check() -> None:
    checks = check_recording(
        _tar(ACTIONS, {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG}, index=False), expected_actions=2
    )
    assert _status(checks, "format:tar") == "fail"


def test_a_path_traversal_member_name_never_passes_as_the_real_file() -> None:
    """`../index.html` must never collapse into `index.html` - the old `.lstrip("./")` bug."""
    checks = check_recording(
        _tar(ACTIONS, {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG}, index_name="../index.html"),
        expected_actions=2,
    )
    assert _status(checks, "format:tar") == "fail"


def test_an_absolute_member_name_never_passes_as_the_real_file() -> None:
    checks = check_recording(
        _tar(ACTIONS, {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG}, index_name="/index.html"),
        expected_actions=2,
    )
    assert _status(checks, "format:tar") == "fail"


def test_the_real_publish_shape_wraps_one_directory_and_still_passes() -> None:
    """`docker_driver.publish` tars a directory as `<basename>/...` - the same single-root
    wrapping `tar_preview` and the code suite's `_project_root` already strip."""
    checks = check_recording(
        _tar(
            ACTIONS,
            {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG},
            prefix="recording-pub-1/",
        ),
        expected_actions=2,
    )
    assert all(c["status"] == "pass" for c in checks), checks


def test_a_row_missing_required_keys_fails_format_actions() -> None:
    incomplete = [dict(ACTIONS[0]), {"seq": 2, "verb": "click"}]
    checks = check_recording(
        _tar(incomplete, {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG}), expected_actions=2
    )
    assert _status(checks, "format:actions") == "fail"


def test_every_check_validates_as_the_pipelines_check_model() -> None:
    """The pipeline does `Check.model_validate(c)` and `Check.expected/actual` are text: a count,
    a list or a dict passed through raw threw, so no recording ever got a verdict."""
    full = {"0001-open.jpg": JPEG, "0002-click.jpg": JPEG}
    runs = {
        "match": check_recording(_tar(ACTIONS, full), expected_actions=2),
        "mismatch": check_recording(_tar(ACTIONS[:1], {"0001-open.jpg": JPEG}), expected_actions=2),
        "missing images": check_recording(_tar(ACTIONS, {"0001-open.jpg": JPEG}), expected_actions=2),
        "no index": check_recording(_tar(ACTIONS, full, index=False), expected_actions=2),
        "non-tar": check_recording(b"not a tar at all" * 100, expected_actions=2),
    }
    for label, checks in runs.items():
        assert checks, label
        for c in checks:
            Check.model_validate(c)
    count = next(c for c in runs["mismatch"] if c["name"] == "format:count")
    assert count["expected"] == "2" and count["actual"] == "1"
    images = next(c for c in runs["missing images"] if c["name"] == "format:images")
    assert images["actual"] == '{"absent": ["0002-click.jpg"], "not_image": []}'
