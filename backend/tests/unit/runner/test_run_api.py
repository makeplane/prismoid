# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid run api` serves the screen's JPEG frames uncompressed: per-message deflate would spend the
one event loop on bytes that do not shrink, once per viewer."""

from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from prismoid.runner.main import app


def test_websockets_are_not_deflated(monkeypatch: pytest.MonkeyPatch) -> None:
    served: dict[str, Any] = {}

    def run(_: str, **kwargs: Any) -> None:
        served.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", run)
    result = CliRunner().invoke(app, ["run", "api"])
    assert result.exit_code == 0, result.output
    assert served["ws_per_message_deflate"] is False
