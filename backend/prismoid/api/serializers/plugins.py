# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plugin installs - responses only. Installing itself is a CLI act (it reads a directory the
server cannot see), so there is no *In shape: the API's whole surface is the receipt."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict

from prismoid.db.models.plugin_install import PluginInstall


class PluginInstallOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    version: str
    items: list[dict[str, Any]]  # the InstallReport's line items, verbatim
    notes: list[str]
    created_at: dt.datetime


def plugin_install_out(record: PluginInstall) -> PluginInstallOut:
    report: dict[str, Any] = record.report or {}
    return PluginInstallOut(
        id=str(record.id),
        name=record.name,
        version=record.version,
        items=[dict(i) for i in report.get("items", [])],
        notes=[str(n) for n in report.get("notes", [])],
        created_at=record.created_at,
    )
