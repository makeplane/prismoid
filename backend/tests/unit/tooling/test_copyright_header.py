# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The copyright header (ADR-102): what the check calls headed, and what `--fix` writes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import copyright_header as ch

_NOTICE = "Copyright (c) 2026-present Example, Inc. and contributors\nSPDX-License-Identifier: X\n"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "COPYRIGHT.txt").write_text(_NOTICE, encoding="utf-8")
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.parametrize(
    ("rel", "headed"),
    [
        ("backend/prismoid/x.py", True),
        ("sandbar/x.go", True),
        ("web/app/x.tsx", True),
        ("web/app/app.css", True),
        ("deploy/deploy.sh", True),
        ("web/app/lib/api/types.gen.ts", False),
        ("web/app/env.d.ts", False),
        ("backend/vendor/some_sdk/x.py", False),
        ("README.md", False),
    ],
)
def test_which_files_carry_the_header(rel: str, headed: bool) -> None:
    assert ch.headed(rel) is headed


def test_each_syntax_gets_its_own_comment() -> None:
    notice = ["A", "B"]
    assert ch.render(ch.HASH, notice) == ["# A", "# B"]
    assert ch.render(ch.SLASHES, notice) == ["// A", "// B"]
    assert ch.render(ch.BLOCK, notice) == ["/**", " * A", " * B", " */"]


def test_the_header_goes_after_a_shebang_then_one_blank_line() -> None:
    header = ["# A"]
    assert ch.with_header("#!/bin/sh\n\nset -eu\n", header) == "#!/bin/sh\n# A\n\nset -eu\n"
    assert ch.with_header('"""Doc."""\n', header) == '# A\n\n"""Doc."""\n'
    assert ch.has_header("#!/bin/sh\n# A\n\nset -eu\n", header)
    assert not ch.has_header("set -eu\n# A\n", header)


def test_the_check_names_every_unheaded_file_and_fix_heads_them(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "b.go": "package b\n",
            "c.tsx": "export {};\n",
            "empty.py": "",
            "notes.md": "no header needed\n",
        },
    )
    assert ch.unheaded(root) == ["a.py", "b.go", "c.tsx"]
    assert ch.fix(root) == []
    assert ch.unheaded(root) == []
    assert (root / "b.go").read_text(encoding="utf-8").startswith("// Copyright (c) 2026-present Example")
    assert ch.fix(root) == []  # idempotent: a headed file is never headed twice


def test_fix_never_covers_a_copyright_already_there(tmp_path: Path) -> None:
    """Someone else's notice is not ours to cover: the file is named for a person, and left as it was."""
    root = _tree(tmp_path, {"third.py": "# Copyright 2019 Someone Else\nx = 1\n"})
    assert ch.fix(root) == ["third.py"]
    assert (root / "third.py").read_text(encoding="utf-8") == "# Copyright 2019 Someone Else\nx = 1\n"
