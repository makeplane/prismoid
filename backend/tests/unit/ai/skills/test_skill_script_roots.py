# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bundled skill scripts find their roots in the environment (the device Machine, ADR-078).

The scripts ship byte-for-byte, so on a device the rendered SKILL.md cannot move them; the
device helper sets PRISMOID_*_DIR instead. Unset (every cloud machine), each root is its virtual one.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from tests.support.paths import BACKEND

SKILLS = BACKEND / "skills"
# script -> {module constant: (env var, virtual root)}
SCRIPTS = {
    "docx/scripts/check_docx.py": {"WORK_DIR": ("PRISMOID_WORK_DIR", "/work")},
    "deck/scripts/check_deck.py": {"WORK_DIR": ("PRISMOID_WORK_DIR", "/work")},
    "pdf/scripts/check_pdf.py": {"WORK_DIR": ("PRISMOID_WORK_DIR", "/work")},
    "model/scripts/check_workbook.py": {"WORK_DIR": ("PRISMOID_WORK_DIR", "/work")},
}


# frame-resident libraries the scripts import at module top; only their root constants are under test
FRAME_LIBS = ("docx", "pptx", "pypdf", "openpyxl")


def _load(rel: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for lib in FRAME_LIBS:
        monkeypatch.setitem(sys.modules, lib, MagicMock())
    spec = importlib.util.spec_from_file_location("skill_script_under_test", SKILLS / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("rel", sorted(SCRIPTS))
def test_roots_default_to_the_virtual_roots(rel: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, _ in (v for roots in SCRIPTS.values() for v in roots.values()):
        monkeypatch.delenv(var, raising=False)
    module = _load(rel, monkeypatch)
    for const, (_, virtual) in SCRIPTS[rel].items():
        assert getattr(module, const) == virtual


@pytest.mark.parametrize("rel", sorted(SCRIPTS))
def test_roots_follow_the_environment(rel: str, monkeypatch: pytest.MonkeyPatch) -> None:
    module_roots = SCRIPTS[rel]
    for _, (var, _) in module_roots.items():
        monkeypatch.setenv(var, "/W")
    module = _load(rel, monkeypatch)
    for const in module_roots:
        assert getattr(module, const) == "/W"
