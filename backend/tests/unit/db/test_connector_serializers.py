# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The connector wire shapes (ADR-020): what crosses, and - the point - what cannot.

`ConnectorOut` is built by explicit constructor, so a new secret column on the row can never
leak by `from_attributes` accident; this file pins the field set both ways.
"""

from __future__ import annotations

from prismoid.api.serializers.connectors import ConnectorCreateIn, ConnectorOut
from prismoid.db.models.connector import Connector

SEALED = {"client_info_enc", "tokens_enc", "flow_enc", "oauth_state_hash", "key_id"}


def test_out_shape_carries_no_sealed_column():
    assert SEALED.isdisjoint(ConnectorOut.model_fields), "the credential is exactly what is not here"
    assert {"id", "name", "slug", "server_url", "status", "auth_kind", "tools"} <= set(
        ConnectorOut.model_fields
    )


def test_every_sealed_column_actually_exists_on_the_row():
    # The guard above is only meaningful while these names ARE the row's secret columns.
    assert set(Connector.__table__.columns.keys()) >= SEALED


def test_create_in_accepts_secrets_but_out_never_echoes_them():
    body = ConnectorCreateIn(
        server_url="https://mcp.example.com/mcp", bearer_token="tok", client_secret="sec"
    )
    assert body.bearer_token == "tok"
    assert {"bearer_token", "client_secret"}.isdisjoint(ConnectorOut.model_fields)
