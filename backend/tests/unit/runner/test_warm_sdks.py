# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The worker loads the chat SDKs at boot, so the first step after a restart never pays their import
(1.2 s measured before the first talk turn's model request)."""

from __future__ import annotations

import inspect
import sys

from prismoid.ai.gateway.providers import warm_sdks
from prismoid.runner import worker


def test_warm_sdks_loads_what_build_model_imports_lazily() -> None:
    loaded = warm_sdks()
    for name in ("openai", "anthropic", "pydantic_ai.models.openai", "pydantic_ai.providers.anthropic"):
        assert name in loaded
        assert name in sys.modules


def test_the_worker_warms_before_it_launches() -> None:
    body = inspect.getsource(worker.main)
    assert body.index("warm_sdks()") < body.index("durable.launch()")
