# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The em-dash gate (ADR-053): the platform's source speaks without em dashes.

Every em dash out of the platform - UI copy, backend copy,
prompts, skills, CLI help, comments - replaced with a comma, colon, middot, or hyphen, and
kept out by a gate on each side (web: `pnpm lint:dash`; backend: this test). The character is
spelled by escape below so this file never trips its own law.

Scope mirrors the purge: `prismoid/`, `skills/`, `evals/`. Excluded, with reasons:
`db/migrations/versions/` (append-only doctrine - applied migrations are frozen, their
docstrings keep their punctuation) and generated/binary files. A line that must carry the
character deliberately (a fixture exercising unicode input) wears an `em-dash-ok` marker on
the same line and is skipped, the token-ok grammar.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.paths import BACKEND

EM_DASH = "\u2014"  # the character under law, spelled by escape
MARKER = "em-dash-ok"

_BACKEND = BACKEND
_ROOTS = [_BACKEND / "prismoid", _BACKEND / "skills", _BACKEND / "evals"]
_FROZEN = _BACKEND / "prismoid" / "db" / "migrations" / "versions"
_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".sh", ".toml", ".txt", ""}


def _in_scope(path: Path) -> bool:
    if _FROZEN in path.parents:
        return False
    if "__pycache__" in path.parts:
        return False
    return path.suffix in _SUFFIXES and path.is_file()


def test_no_em_dash_in_source() -> None:
    offenders: list[str] = []
    for root in _ROOTS:
        for path in sorted(root.rglob("*")):
            if not _in_scope(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if EM_DASH in line and MARKER not in line:
                    offenders.append(f"{path.relative_to(_BACKEND)}:{lineno}")
    assert not offenders, (
        "em dash found - the platform is em-dash-free (ADR-053 W0); use a comma, colon, "
        "middot, or hyphen, or mark a deliberate fixture line with 'em-dash-ok':\n"
        + "\n".join(offenders[:40])
    )
