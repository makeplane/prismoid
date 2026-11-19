# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The observability group: where spans go and what the process prints (ADR-101).

`prismoid.conf` composes this into `settings` (`settings.observability`) and every caller reads it there.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

from prismoid.conf_env import cfg

LogFormat = Literal["json", "console"]
LogLevel = Literal["debug", "info", "warning", "error"]


class ObservabilitySettings(BaseSettings):
    """Unprefixed by convention - these are the ecosystem's names, not ours; the two log settings
    are ours and say so (`PRISMOID_LOG_*`)."""

    model_config = cfg()

    logfire_token: SecretStr = SecretStr("")
    otel_exporter_otlp_endpoint: str = ""
    service_name: str = "prismoid"
    # json: stdout carries log records only, one JSON object per line, and spans leave only by
    # OTLP. console: Logfire's console prints every span, as on a laptop. Unset: json in staging and
    # prod, console elsewhere (`stdout_log.log_format`).
    log_format: LogFormat | None = Field(default=None, validation_alias="PRISMOID_LOG_FORMAT")
    # The least severe record stdout carries in json format.
    log_level: LogLevel = Field(default="info", validation_alias="PRISMOID_LOG_LEVEL")
