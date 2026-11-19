# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The engine's one door to deployment settings.

`ai/` takes values in and hands values back, so a frame driver, a provider wire or a signer must
not reach for the process-wide `prismoid.conf.settings` on its own. Every read in `ai/` goes through
`current()` instead, at call time: nothing captures a value at import, a test or a host embedding
the engine installs its own `Settings` with `use_settings`, and this module is the only file in
`ai/` that imports `prismoid.conf` (the `.importlinter` contract "ai/ reads deployment settings only
through prismoid.ai.config" holds that).

With nothing installed, `current()` is the deployment's own `settings`, the very object the rest of
the stack reads and tests patch in place, so an override here and a `monkeypatch` there agree.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from prismoid.conf import GatewaySettings, SearchSettings, Settings
from prismoid.conf import settings as _deployment

__all__ = ["GatewaySettings", "SearchSettings", "Settings", "current", "use_settings"]

_installed: Settings | None = None


def current() -> Settings:
    """The settings the engine runs under: the installed ones, else the deployment's."""
    return _installed if _installed is not None else _deployment


@contextmanager
def use_settings(settings: Settings) -> Generator[Settings]:
    """Run the engine under `settings` until the block exits, then restore what was there.

    Process-wide on purpose: frame drivers and provider wires run on worker threads too, where a
    context variable set by the caller would not follow."""
    global _installed
    previous = _installed
    _installed = settings
    try:
        yield settings
    finally:
        _installed = previous
