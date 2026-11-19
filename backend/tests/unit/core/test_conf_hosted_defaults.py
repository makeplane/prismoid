# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A hosted box boots safe or not at all: open signup is an operator's explicit choice in staging
and production, and the dev compose's well-known credentials refuse to boot there. No stack
needed - a pure Settings suite."""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from prismoid.conf import Settings
from tests.support.hosted import use_real_secrets
from tests.support.settings_env import no_developer_settings


@pytest.fixture(autouse=True)
def bare_process(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Every default asserted here is the shipped one, not the developer's `.env` or shell."""
    no_developer_settings(monkeypatch, tmp_path)


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """PRISMOID_ENV=prod with every secret real: each test then breaks exactly one thing."""
    monkeypatch.setenv("PRISMOID_ENV", "prod")
    monkeypatch.setenv("PRISMOID_EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("PRISMOID_COMPUTER_USE_ENABLED", "false")
    monkeypatch.setenv("PRISMOID_WORKER_FRAME_DRIVER", "docker")
    use_real_secrets(monkeypatch)
    monkeypatch.delenv("PRISMOID_AUTH_SIGNUP_ENABLED", raising=False)
    return monkeypatch


def test_signup_is_closed_on_a_hosted_box_unless_the_operator_opens_it(hosted: pytest.MonkeyPatch) -> None:
    assert Settings().auth.signup_enabled is False
    hosted.setenv("PRISMOID_AUTH_SIGNUP_ENABLED", "true")
    assert Settings().auth.signup_enabled is True


@pytest.mark.parametrize("env", ["dev", "test"])
def test_signup_stays_open_by_default_in_dev(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("PRISMOID_ENV", env)
    monkeypatch.delenv("PRISMOID_AUTH_SIGNUP_ENABLED", raising=False)
    assert Settings().auth.signup_enabled is True


def test_staging_reads_signup_as_closed_too(hosted: pytest.MonkeyPatch) -> None:
    hosted.setenv("PRISMOID_ENV", "staging")
    assert Settings().auth.signup_enabled is False


def test_real_secrets_boot(hosted: pytest.MonkeyPatch) -> None:
    assert Settings().environment == "prod"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PRISMOID_WORKER_S3_SECRET_KEY", "prismoid1234"),
        ("PRISMOID_API_DATABASE_URL", "postgresql+asyncpg://prismoid:prismoid@db:5432/prismoid"),
        ("PRISMOID_WORKER_DATABASE_URL", "postgresql://prismoid:prismoid@db:5432/prismoid"),
        ("PRISMOID_EGRESS_CONTROL_SECRET", "dev-egress-control-secret"),
        ("PRISMOID_EGRESS_CONTROL_SECRET", ""),
    ],
)
def test_a_dev_secret_refuses_to_boot_and_is_named_never_echoed(
    hosted: pytest.MonkeyPatch, name: str, value: str
) -> None:
    hosted.setenv(name, value)
    with pytest.raises(ValidationError) as caught:
        Settings()
    message = str(caught.value)
    assert name in message
    assert "prismoid1234" not in message and "dev-egress-control-secret" not in message


def test_the_egress_secret_matters_only_where_docker_frames_run(hosted: pytest.MonkeyPatch) -> None:
    hosted.setenv("PRISMOID_EGRESS_CONTROL_SECRET", "")
    hosted.setenv("PRISMOID_WORKER_FRAME_DRIVER", "sandbar")
    hosted.setenv("PRISMOID_WORKER_SANDBAR_TOKEN", "a-real-sandbar-token")
    assert Settings().worker.frame_driver == "sandbar"


def test_a_process_that_makes_no_frames_needs_no_frame_secret(hosted: pytest.MonkeyPatch) -> None:
    """The shipped compose names the driver on the worker only; the API boots without the worker's
    frame secrets."""
    hosted.delenv("PRISMOID_WORKER_FRAME_DRIVER")
    hosted.setenv("PRISMOID_EGRESS_CONTROL_SECRET", "")
    hosted.delenv("PRISMOID_WORKER_SANDBAR_TOKEN", raising=False)
    assert Settings().environment == "prod"


def test_a_sandbar_worker_needs_its_token(hosted: pytest.MonkeyPatch) -> None:
    hosted.setenv("PRISMOID_WORKER_FRAME_DRIVER", "sandbar")
    hosted.delenv("PRISMOID_WORKER_SANDBAR_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="PRISMOID_WORKER_SANDBAR_TOKEN"):
        Settings()
    hosted.setenv("PRISMOID_WORKER_SANDBAR_TOKEN", "a-real-sandbar-token")
    assert Settings().worker.frame_driver == "sandbar"


def test_the_s3_secret_matters_only_with_the_s3_store(hosted: pytest.MonkeyPatch) -> None:
    """Local mode keeps objects on disk and never boots as prod; the check reads the backend."""
    hosted.setenv("PRISMOID_WORKER_S3_SECRET_KEY", "prismoid1234")
    with pytest.raises(ValidationError, match="PRISMOID_WORKER_S3_SECRET_KEY"):
        Settings()


def test_dev_keeps_its_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMOID_ENV", "dev")
    assert Settings().worker.s3_secret_key.get_secret_value()  # the compose default boots in dev


def test_sandbar_backend_defaults_to_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker is the one sandbar backend that ships; Firecracker is opt-in and experimental."""
    monkeypatch.delenv("PRISMOID_WORKER_SANDBAR_BACKEND", raising=False)
    assert Settings().worker.sandbar_backend == "docker"
