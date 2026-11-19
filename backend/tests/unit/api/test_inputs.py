# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Task inputs - the value, and the one rule that must never bend.

A filename is a *name*. The moment it can also be a path, `/task/inputs/` stops being a directory
the system controls and starts being an address the caller chooses - and `../../../work/memo.md` is
an attachment that overwrites the agent's own output before it is published.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prismoid.ai.frames.layout import INPUTS, input_path, input_paths
from prismoid.api.serializers.tasks import TaskInputIn
from prismoid.schemas.inputs import InputSet, RawInput, TaskInput

# Every one of these is "just a filename" to code that does not look.
TRAVERSALS = [
    "../secrets.md",
    "../../etc/passwd",
    "/etc/passwd",
    "notes/../../../work/memo.md",
    "dir/file.md",
    "",
    ".",
    "..",
    ".hidden",  # a leading dot is not a traversal, but it is not a name a user typed either
]


@pytest.mark.parametrize("name", TRAVERSALS)
def test_a_filename_may_not_be_a_path(name: str) -> None:
    with pytest.raises(ValidationError):
        TaskInput(filename=name, sha256="a" * 64, bytes=1)
    with pytest.raises(ValidationError):
        RawInput(filename=name, data=b"x")


@pytest.mark.parametrize("name", TRAVERSALS)
def test_the_request_schema_rejects_it_too_so_it_is_a_422_and_not_a_500(name: str) -> None:
    """**The door needs the lock, not just the vault.**

    The validator lived only on the inner value, so `../../etc/passwd` passed the request schema,
    raised while building `RawInput` *inside* the endpoint, and came back as an HTTP **500**. The
    attack was refused - and still produced a server error and a stack trace for the attacker's
    trouble. Validating at the boundary makes it the 422 it always was. Both checks stay: the
    serializer is the door, the value is the lock, and neither is the other's excuse.
    """
    with pytest.raises(ValidationError):
        TaskInputIn(filename=name, content="aGk=")


@pytest.mark.parametrize("name", ["brief.md", "Q3-2026_report.pdf", "a", "x.tar.gz"])
def test_an_ordinary_filename_is_fine(name: str) -> None:
    assert TaskInput(filename=name, sha256="a" * 64, bytes=1).filename == name


def test_the_frame_path_is_built_by_the_frame_layer() -> None:
    """`schemas` sits below `ai`: the value holds the name, `layout` builds the path. One
    definition of where an input lives, in the module that owns the frame's layout."""
    assert input_path("brief.md") == f"{INPUTS}/brief.md"
    assert input_paths(["a.md", "b.md"]) == (f"{INPUTS}/a.md", f"{INPUTS}/b.md")


def test_an_empty_input_set_is_falsy() -> None:
    """So `if inputs:` reads as "were there sources?" - the question the verifier actually asks."""
    assert not InputSet()
    assert InputSet(items=[TaskInput(filename="a.md", sha256="a" * 64, bytes=1)])


@pytest.mark.parametrize(
    "name",
    [
        "CleanShot 2026-08-02 at 00.14.21@2x.png",  # the 422 that started this (projects, @)
        "résumé.pdf",  # unicode letters are letters
        "my report (final).docx",
        "IMG_8038.JPG",
    ],
)
def test_what_humans_call_files_is_a_filename(name: str) -> None:
    assert TaskInput(filename=name, sha256="a" * 64, bytes=1).filename == name
    assert TaskInputIn(filename=name, content="aGk=").filename == name


@pytest.mark.parametrize(
    "name",
    [
        "it's.png",  # a single quote would let repr() switch quoting styles - banned
        'say "hi".md',
        "a$b.txt",
        "`whoami`.txt",
        "a;b.txt",
        "-rf.txt",  # flag injection
        "trailing .txt ",  # trailing space
    ],
)
def test_shell_dangerous_names_stay_banned(name: str) -> None:
    with pytest.raises(ValidationError):
        RawInput(filename=name, data=b"x")
