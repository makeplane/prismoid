# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The public-tree law: private folders and private repo names stay out of the tree.

Prismoid's public repository is published with a fresh history. The audits, plans, specs, logs,
research and archive that shaped it, and the names of Makeplane's private repositories, stay in
Makeplane's private archive. Two checks keep them out for good: no tracked file lives under one
of the six private folders, and no tracked file outside `backend/vendor/` names a private repo,
in any case and in any file, binary ones included (the scan reads bytes). This file holds the
pattern, so it is excluded from its own scan.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SELF = "backend/tests/laws/test_law_public_tree.py"
_PRIVATE_FOLDERS = (
    "docs/audit",
    "docs/superpowers",
    "docs/logs",
    "docs/research",
    "docs/archive",
    "docs/human",
)
_PRIVATE_NAMES = re.compile(
    rb"makeplane/computerd|helm-charts-private|fleet-deployments|plane-ee|plane-cloud", re.IGNORECASE
)


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [p for p in out.split("\0") if p]


def test_no_private_folder_returns() -> None:
    offenders = [p for p in _tracked() if p.startswith(tuple(f + "/" for f in _PRIVATE_FOLDERS))]
    assert not offenders, "private folders must stay out of the public tree:\n" + "\n".join(offenders[:40])


def test_no_private_repo_name() -> None:
    offenders: list[str] = []
    for rel in _tracked():
        if rel == _SELF or rel.startswith("backend/vendor/"):
            continue
        path = _ROOT / rel
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for lineno, line in enumerate(data.splitlines(), start=1):
            if _PRIVATE_NAMES.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, "a private repo name is in the public tree:\n" + "\n".join(offenders[:40])
