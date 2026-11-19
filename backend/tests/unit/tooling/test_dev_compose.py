# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dev stack's compose file - `just up`, and the workflows that bring the same stack up on a
runner. A static read: a password the object store refuses is a dead `just up` with nothing in the
code to show for it, and it happened (the rename shortened the root password to 7 characters)."""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import SecretStr

from prismoid.conf import WorkerSettings
from tests.support.paths import BACKEND, REPO

COMPOSE = BACKEND / "deploy" / "compose.yaml"
PROD_COMPOSE = REPO / "deploy" / "compose.prod.yaml"


def _services() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_minio_root_password_is_one_minio_accepts_and_the_worker_uses() -> None:
    services = _services()
    password = str(services["minio"]["environment"]["MINIO_ROOT_PASSWORD"])
    assert len(password) >= 8, (
        "the object store (Silo, like MinIO) refuses a root password under 8 characters"
    )
    default = WorkerSettings.model_fields["s3_secret_key"].default
    assert isinstance(default, SecretStr) and default.get_secret_value() == password
    assert f"prismoid {password} " in str(services["minio-init"]["entrypoint"])


def test_production_has_no_fallback_passwords() -> None:
    """compose.prod.yaml requires every password: a forgotten one fails `config`, never boots on a
    credential printed in this repository."""
    text = PROD_COMPOSE.read_text()
    dev_password = str(_services()["minio"]["environment"]["MINIO_ROOT_PASSWORD"])
    for secret in ("POSTGRES_PASSWORD", "MINIO_PASSWORD", "SANDBAR_TOKEN"):
        assert f"${{{secret}:-" not in text, f"{secret} has a fallback in compose.prod.yaml"
        assert f"${{{secret}:?" in text, f"{secret} is not required in compose.prod.yaml"
    assert dev_password not in text


def test_every_stack_runs_the_same_pinned_object_store() -> None:
    """The dev stack, its bucket step, production and `just e2e` run one Silo image, pinned by digest:
    a bump that misses one of them runs two servers that may disagree on the data."""
    image = str(_services()["minio"]["image"])
    assert image.startswith("pgsty/silo:") and "@sha256:" in image
    assert _services()["minio-init"]["image"] == image
    prod = yaml.safe_load(PROD_COMPOSE.read_text())["services"]
    assert prod["minio"]["image"] == image and prod["minio-init"]["image"] == image
    stack = (COMPOSE.parents[2] / "web" / "e2e" / "stack.sh").read_text()
    assert f'SILO_IMAGE="{image}"' in stack


def test_the_dev_stack_host_ports_can_move() -> None:
    """A second stack (another checkout, a test run) must be able to sit beside the first: every port
    `just up` publishes is a variable whose default is today's port."""
    defaults = {
        "postgres": "PRISMOID_DB_PORT:-5433",
        "minio": "PRISMOID_MINIO_PORT:-9000",
        "egress-proxy": "PRISMOID_EGRESS_CONTROL_PORT:-3129",
    }
    services = _services()
    for name, default in defaults.items():
        assert any(default in str(port) for port in services[name]["ports"]), name
        for port in services[name]["ports"]:
            assert str(port).startswith("127.0.0.1:${"), f"{name} publishes a fixed host port: {port}"
