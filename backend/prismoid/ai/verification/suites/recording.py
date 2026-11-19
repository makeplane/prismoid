# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""recording-suite: structural, no model. The tar's root carries index.html and
actions.json; every referenced image exists and is an image; the action count equals what
the engine's journal saw for the attempt (passed in as context); no action row carries a
typed value."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from typing import TypeGuard

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check
MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")
MAGIC_BYTES = max(len(m) for m in MAGIC)  # read only this many bytes per image, never the whole file
REQUIRED = ("verb", "target", "host", "path", "at", "file")
FORBIDDEN = ("value", "text", "fields", "query")


def _check(
    name: str,
    kind: str,
    status: str,
    detail: str = "",
    *,
    expected: object | None = None,
    actual: object | None = None,
) -> dict[str, object]:
    """One check as the pipeline parses it (`schemas.verification.Check`), `expected`/`actual` as text
    (JSON for a collection); stdlib only, it runs inside the frame."""
    return {
        "name": name,
        "kind": kind,
        "status": status,
        "detail": detail,
        "expected": _text(expected),
        "actual": _text(actual),
    }


def _text(value: object | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def check_recording(tar_bytes: bytes, expected_actions: int | None) -> list[dict[str, object]]:
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tf:
            return _check_open_tar(tf, expected_actions)
    except tarfile.TarError as bad:
        return [_check("format:tar", STRUCTURAL, FAIL, "not a tar", actual=str(bad))]


def _safe_members(tf: tarfile.TarFile) -> tuple[dict[str, tarfile.TarInfo], int]:
    """Every regular-file member keyed by its name minus one leading `./`; absolute names, `..`
    components and non-files are dropped and counted, never renamed."""
    safe: dict[str, tarfile.TarInfo] = {}
    unsafe = 0
    for m in tf.getmembers():
        if not m.isfile():
            continue
        if m.name.startswith("/") or ".." in m.name.split("/"):
            unsafe += 1
            continue
        safe[m.name.removeprefix("./")] = m
    return safe, unsafe


def _check_open_tar(tf: tarfile.TarFile, expected_actions: int | None) -> list[dict[str, object]]:
    raw_members, unsafe = _safe_members(tf)
    members = _project_members(raw_members)
    names = set(members)
    note = f"; {unsafe} unsafe member name(s) ignored" if unsafe else ""

    if "index.html" not in names or "actions.json" not in names:
        return [
            _check(
                "format:tar",
                STRUCTURAL,
                FAIL,
                "index.html and actions.json must sit at the root" + note,
                actual=sorted(names)[:10],
            )
        ]
    checks = [
        _check(
            "format:tar",
            STRUCTURAL,
            PASS,
            f"{len(names)} file(s), index.html and actions.json present" + note,
        )
    ]

    try:
        # The rows are taken as objects on trust, as they always were: a non-object row fails
        # format:actions by its missing keys, and `_images_check` would raise on it (-> suite:ran).
        actions: list[dict[str, object]] = json.loads(
            _read_member(tf, members["actions.json"]).decode("utf-8")
        )
        assert isinstance(actions, list)
    except (ValueError, AssertionError, UnicodeDecodeError) as bad:
        return [
            *checks,
            _check("format:actions", STRUCTURAL, FAIL, "actions.json is not a JSON list", actual=str(bad)),
        ]
    checks.append(_actions_check(actions))
    checks.append(_images_check(tf, members, actions))
    checks.append(_count_check(len(actions), expected_actions))
    checks.append(_values_check(actions))
    return checks


def _project_members(raw_members: dict[str, tarfile.TarInfo]) -> dict[str, tarfile.TarInfo]:
    raw_names = set(raw_members)
    # The publish door tars a directory as `<basename>/...` (`docker_driver.publish`) - the same
    # shape `tar_preview`'s `page` kind and the code suite's `_project_root` already strip. A
    # flat tar (no wrapping directory, as the unit tests build) is root already; either way,
    # "at the root" means root of the PROJECT, not necessarily of the tar file. Exactly one such
    # wrapping component is stripped, never more.
    roots = {n.split("/", 1)[0] for n in raw_names}
    prefix = f"{next(iter(roots))}/" if len(roots) == 1 and all("/" in n for n in raw_names) else ""
    return {n[len(prefix) :]: m for n, m in raw_members.items()}


def _read_member(tf: tarfile.TarFile, member: tarfile.TarInfo, limit: int | None = None) -> bytes:
    fh = tf.extractfile(member)
    if fh is None:
        return b""
    return fh.read() if limit is None else fh.read(limit)


def _actions_check(actions: list[dict[str, object]]) -> dict[str, object]:
    missing = [i for i, a in enumerate(actions) if not all(k in a for k in REQUIRED)]
    return _check(
        "format:actions",
        STRUCTURAL,
        FAIL if missing else PASS,
        f"{len(actions)} action(s)" + (f"; rows missing keys: {missing[:5]}" if missing else ""),
    )


def _images_check(
    tf: tarfile.TarFile, members: dict[str, tarfile.TarInfo], actions: list[dict[str, object]]
) -> dict[str, object]:
    # A row with `file == ""` is the daemon's failed-capture placeholder: the action happened
    # (the journal counted it, so `format:count` still does), only its picture is missing -
    # skipped here by name and reported, never a missing image.
    absent: list[str] = []
    not_image: list[str] = []
    failed = 0
    for a in actions:
        f = str(a.get("file", ""))
        if f == "":
            failed += 1
        elif f not in members:
            absent.append(f)
        elif not _read_member(tf, members[f], limit=MAGIC_BYTES).startswith(MAGIC):
            not_image.append(f)
    return _check(
        "format:images",
        STRUCTURAL,
        FAIL if absent or not_image else PASS,
        f"{len(actions) - failed - len(absent) - len(not_image)} image(s) present"
        + (f"; {failed} capture(s) failed" if failed else ""),
        actual={"absent": absent[:5], "not_image": not_image[:5]} if absent or not_image else None,
    )


def _count_check(count: int, expected_actions: int | None) -> dict[str, object]:
    if expected_actions is None:
        return _check("format:count", STRUCTURAL, SKIP, "no journal count was supplied")
    return _check(
        "format:count",
        STRUCTURAL,
        PASS if count == expected_actions else FAIL,
        "action count equals the journal's"
        if count == expected_actions
        else "action count disagrees with the journal",
        expected=expected_actions,
        actual=count,
    )


def _values_check(actions: list[dict[str, object]]) -> dict[str, object]:
    leaks = [i for i, a in enumerate(actions) if any(k in a for k in FORBIDDEN)]
    return _check(
        "format:no-values",
        STRUCTURAL,
        FAIL if leaks else PASS,
        "no typed values in the action log" if not leaks else f"rows carry values: {leaks[:5]}",
    )


def run(job: dict[str, object]) -> list[dict[str, object]]:
    artifact_path = str(job["artifact_path"])
    with open(artifact_path, "rb") as fh:
        data = fh.read()
    return check_recording(data, _expected_actions(job.get("context")))


def _expected_actions(context: object) -> int | None:
    """The journal's action count from the job's context, if it carries one."""
    expected = context.get("expected_actions") if _is_object(context) else None
    if expected is None:
        return None
    if not isinstance(expected, int | float | str):
        raise TypeError(f"expected_actions must be a number, not {type(expected).__name__}")
    return int(expected)


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    """A JSON object - its keys are strings by construction (the job is `json.loads` output)."""
    return isinstance(value, dict)


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        checks = run(job)
    except Exception as exc:
        checks = [_check("suite:ran", STRUCTURAL, FAIL, f"{type(exc).__name__}: {exc}")]
    json.dump({"checks": checks}, sys.stdout)


if __name__ == "__main__":
    main()
