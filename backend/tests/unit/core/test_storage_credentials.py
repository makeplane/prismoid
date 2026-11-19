# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The S3 store's credentials: a configured key pair goes to the client as is; an empty pair leaves
boto3 its default credential chain (the pod's IAM role on EKS), never an empty key it would send."""

from typing import Any

import pytest
from pydantic import SecretStr

from prismoid import storage
from prismoid.conf import settings


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    made: list[dict[str, Any]] = []

    class _Client:
        def put_object(self, **_: Any) -> None:
            return None

    def client(service: str, **kwargs: Any) -> _Client:
        made.append({"service": service, **kwargs})
        return _Client()

    monkeypatch.setattr(storage.boto3, "client", client)
    monkeypatch.setattr(settings.worker, "storage_backend", "s3")
    return made


def test_a_configured_key_pair_is_used(
    monkeypatch: pytest.MonkeyPatch, clients: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings.worker, "s3_endpoint", "http://pair.test:9000")
    monkeypatch.setattr(settings.worker, "s3_access_key", "prismoid")
    monkeypatch.setattr(settings.worker, "s3_secret_key", SecretStr("prismoid1234"))
    storage.put(b"x", bucket="b")
    assert clients[-1]["aws_access_key_id"] == "prismoid"
    assert clients[-1]["aws_secret_access_key"] == "prismoid1234"


def test_an_empty_key_pair_leaves_the_default_chain(
    monkeypatch: pytest.MonkeyPatch, clients: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings.worker, "s3_endpoint", "https://s3.role.test")
    monkeypatch.setattr(settings.worker, "s3_access_key", "")
    monkeypatch.setattr(settings.worker, "s3_secret_key", SecretStr(""))
    storage.put(b"x", bucket="b")
    assert clients[-1]["aws_access_key_id"] is None
    assert clients[-1]["aws_secret_access_key"] is None


@pytest.mark.parametrize(
    ("endpoint", "region"),
    [
        ("https://s3.us-east-2.amazonaws.com", "us-east-2"),
        ("https://s3-eu-west-1.amazonaws.com", "eu-west-1"),
        ("http://minio.region.test:9000", "us-east-1"),
    ],
)
def test_requests_are_signed_for_the_endpoints_region(
    monkeypatch: pytest.MonkeyPatch, clients: list[dict[str, Any]], endpoint: str, region: str
) -> None:
    monkeypatch.setattr(settings.worker, "s3_endpoint", endpoint)
    storage.put(b"x", bucket="b")
    assert clients[-1]["region_name"] == region
