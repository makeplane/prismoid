# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ADR-031 boot gate: console mail refuses to boot anywhere shared, and the backend field
rejects at startup what the mailer could never dispatch - a typo'd backend was a silent total
outage of account recovery (202s forever, mail never), and console-in-prod printed users' reset
codes into whoever reads the logs. No stack needed - a pure Settings suite.
"""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from prismoid.conf import EmailSettings, Settings
from tests.support.hosted import use_real_secrets
from tests.support.settings_env import no_developer_settings


def test_console_backend_refuses_to_boot_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set via PRISMOID_ENV - the spelling the shipped prod compose uses - which also pins the
    # alias fix: before it, PRISMOID_ENV was a variable nothing read and every deploy was "dev".
    monkeypatch.setenv("PRISMOID_ENV", "prod")
    with pytest.raises(ValidationError, match="refused"):
        Settings(email=EmailSettings(backend="console"))


def test_console_backend_refuses_staging_via_the_long_spelling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    no_developer_settings(monkeypatch, tmp_path)  # an exported PRISMOID_ENV would outrank the long spelling
    monkeypatch.setenv("PRISMOID_ENVIRONMENT", "staging")
    with pytest.raises(ValidationError, match="refused"):
        Settings(email=EmailSettings(backend="console"))


def test_smtp_boots_in_prod_and_console_stays_the_dev_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A shell or a backend/.env that turns computer use on would meet the screen key's prod check
    # (the screen key's check); the process env outranks the file.
    monkeypatch.setenv("PRISMOID_COMPUTER_USE_ENABLED", "false")
    monkeypatch.setenv("PRISMOID_ENV", "prod")
    use_real_secrets(monkeypatch)
    assert Settings(email=EmailSettings(backend="smtp")).environment == "prod"
    monkeypatch.setenv("PRISMOID_ENV", "dev")
    assert Settings(email=EmailSettings(backend="console")).email.backend == "console"


def test_backend_is_case_insensitive_and_a_typo_dies_at_boot() -> None:
    smtp = EmailSettings(backend="SMTP")  # pyright: ignore[reportArgumentType]  # the validator lowers it
    assert smtp.backend == "smtp"
    with pytest.raises(ValidationError):
        EmailSettings(backend="sendgrid")  # pyright: ignore[reportArgumentType]  # exactly the point
