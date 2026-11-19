# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The push group boots honestly (ADR-100): push is on only where the operator sets the key,
every other box boots with it off, staging and prod included, and a
malformed key fails any boot without echoing it. No stack needed."""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from prismoid.conf import Settings
from tests.support.hosted import use_real_secrets
from tests.support.settings_env import no_developer_settings

_KEY = "PRISMOID_PUSH_CREDENTIALS_JSON"


@pytest.fixture(autouse=True)
def bare_process(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    no_developer_settings(monkeypatch, tmp_path)


_SERVICE_ACCOUNT = (
    '{"type": "service_account", "project_id": "a-real-project", '
    '"client_email": "push@a-real-project.iam.gserviceaccount.com", "private_key": "a-real-key"}'
)


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_a_hosted_box_boots_with_push_off_without_the_key_and_on_with_it(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setenv("PRISMOID_ENV", env)
    monkeypatch.setenv("PRISMOID_EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("PRISMOID_COMPUTER_USE_ENABLED", "false")
    use_real_secrets(monkeypatch)
    assert not Settings().push.enabled(), "no key: push is off, the boot goes on"

    monkeypatch.setenv(_KEY, _SERVICE_ACCOUNT)
    assert Settings().push.enabled()


@pytest.mark.parametrize("env", ["dev", "test", "local"])
def test_dev_test_and_local_run_with_push_off(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("PRISMOID_ENV", env)
    if env == "local":
        monkeypatch.setenv("PRISMOID_WORKER_FRAME_DRIVER", "device")
        monkeypatch.setenv("PRISMOID_WORKER_STORAGE_BACKEND", "fs")
    push = Settings().push
    assert not push.enabled()
    assert push.service_account() is None


@pytest.mark.parametrize(
    "value", ["not json", '["a list"]', '{"project_id": "p", "client_email": "c@p"}', '{"private_key": "k"}']
)
def test_a_value_that_is_not_a_service_account_fails_the_boot_unechoed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_KEY, value)
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert "not a Firebase service account key" in str(caught.value)
    assert value not in str(caught.value)


def test_the_project_comes_from_the_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        _KEY, '{"project_id": "prismoid-push", "client_email": "a@b.iam", "private_key": "-----BEGIN"}'
    )
    account = Settings().push.service_account()
    assert account is not None
    assert account["project_id"] == "prismoid-push"
