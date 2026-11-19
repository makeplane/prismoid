# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The engine's settings door: `ai/config.py` hands every reader in `ai/` the settings in force.

With nothing installed the engine reads the deployment's own object, so a test that patches
`prismoid.conf.settings` in place still steers it; a host installs another for a block and gets the
old one back after, even when the block raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from prismoid.ai import config
from prismoid.ai.frames.device_client import helper_socket
from prismoid.ai.gateway.providers import api_key_for
from prismoid.conf import Settings, settings


def test_nothing_installed_reads_the_deployment() -> None:
    assert config.current() is settings


def test_an_in_place_patch_reaches_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "anthropic_api_key", SecretStr("sk-patched"))
    assert api_key_for("anthropic") == "sk-patched"


def test_an_installed_settings_steers_the_engine_and_is_restored(tmp_path: Path) -> None:
    mine = settings.model_copy(deep=True)
    mine.worker.anthropic_api_key = SecretStr("sk-installed")
    mine.local.home = tmp_path
    with config.use_settings(mine) as installed:
        assert installed is mine and config.current() is mine
        assert api_key_for("anthropic") == "sk-installed"
        assert helper_socket() == str(tmp_path / "run" / "helper.sock")
    assert config.current() is settings


def test_a_raising_block_still_restores() -> None:
    with pytest.raises(RuntimeError), config.use_settings(Settings()):
        raise RuntimeError("boom")
    assert config.current() is settings
