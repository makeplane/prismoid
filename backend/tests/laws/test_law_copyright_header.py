# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The copyright header law (ADR-102): every tracked source file opens with COPYRIGHT.txt's notice.

The population is the tree itself (`git ls-files`, filtered by `tools/copyright_header.headed`), so a
new file is covered the moment it is tracked. `just copyright-fix` writes the header; the Copyright
workflow runs the same check on every pull request and annotates each file it names.
"""

from __future__ import annotations

from tests.support.paths import REPO
from tools import copyright_header


def test_every_source_file_carries_the_copyright_header() -> None:
    missing = copyright_header.unheaded(REPO)
    assert not missing, (
        f"{len(missing)} file(s) without the copyright header (`just copyright-fix` writes it):\n"
        + "\n".join(missing[:40])
    )
