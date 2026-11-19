# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`just evals` asks the server in use for its own token, the CLI's lookup and no other."""

import json
import pathlib

import pytest

from evals import run
from prismoid.conf import CliSettings
from prismoid.runner import client
from tests.support.settings_env import no_developer_settings


def _credentials(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, **data: object) -> None:
    """Only the credentials file written here speaks: no exported key or address, no `.env`, and a
    CLI group built in that bare process (the module-wide one was built from `.env` at import)."""
    no_developer_settings(monkeypatch, tmp_path)
    path = tmp_path / "credentials"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(client, "CREDENTIALS", path)
    monkeypatch.setattr(client.settings, "cli", CliSettings())


def test_the_server_in_use_gets_its_own_token_not_the_earlier_pair(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(
        tmp_path,
        monkeypatch,
        tokens={"http://127.0.0.1:1": "ck_earlier", "http://127.0.0.1:2": "ck_in_use"},
        active="http://127.0.0.1:2",
        token="ck_earlier",
        api_url="http://127.0.0.1:1",
    )
    assert client.api_url() == "http://127.0.0.1:2"
    assert run.api_token() == "ck_in_use"


def test_no_token_for_the_server_in_use_says_so(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(tmp_path, monkeypatch, tokens={}, active="http://127.0.0.1:2")
    with pytest.raises(SystemExit, match=r"127\.0\.0\.1:2"):
        run.api_token()
