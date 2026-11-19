# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`patch_problem`, line by line: each way a text stops being a diff is named with the line it
stops at, never its content, and a git header counts as a change only when it says one."""

from __future__ import annotations

import pytest

from prismoid.ai.verification.change import patch_problem

HUNK = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "it changes nothing"),
        ("a commit message\n\nmore of it\n", "it changes nothing"),
        ("--- a/x\n+++ b/x\n", "it changes nothing"),
        ("diff --git a/x b/x\nindex 1..2 100644\n", "it changes nothing"),
        ("--- a/x\n+++ b/x\n@@ broken @@\n", "line 3: a broken hunk header"),
        ("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n", "line 2: a hunk of no file"),
        ("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n", "the last hunk stops short of its header"),
        ("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n*\n", "line 5: the hunk stops short of its header"),
        ("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n-c\n", "line 5: the hunk runs past its header"),
        (HUNK + "prose after the diff\n", "line 6: not part of a diff"),
        ("diff --git a/i b/i\nGIT binary patch\nnot binary\n", "line 3: not a binary patch"),
    ],
)
def test_each_break_is_named_by_its_line(text: str, problem: str) -> None:
    assert patch_problem(text) == problem


@pytest.mark.parametrize(
    "text",
    [
        HUNK,
        HUNK + "\n",  # a blank line between files
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n",
        "diff --git a/x b/x\ndeleted file mode 100644\n",
        "diff --git a/x b/y\nrename from x\nrename to y\n",
        "diff --git a/i b/i\nGIT binary patch\nliteral 0\nHcmV?d00001\n\n" + "diff --git a/x b/x\n" + HUNK,
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n--- a/y\n+++ b/y\n@@ -0,0 +1 @@\n+c\n",
    ],
)
def test_real_diffs_have_no_problem(text: str) -> None:
    assert patch_problem(text) is None


def test_a_truncated_read_drops_its_fragment_and_may_end_mid_hunk() -> None:
    cut = "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n-b\n+c\nfragm"
    assert patch_problem(cut, truncated=True) is None
    assert patch_problem(cut) == "line 7: the hunk stops short of its header"


def test_a_lone_minus_header_is_not_a_file() -> None:
    assert patch_problem("--- a/x\nnot the plus line\n") == "it changes nothing"
    assert patch_problem("--- a/x\n@@ -1 +1 @@\n-a\n+b\n") == "line 2: a hunk of no file"
