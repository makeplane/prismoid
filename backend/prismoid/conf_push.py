# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The push group: how the worker reaches Firebase Cloud Messaging (ADR-100).

`prismoid.conf` composes this into `settings` (`settings.push`) and every caller reads it there.
"""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings

from prismoid.conf_env import cfg

_SERVICE_ACCOUNT_KEYS = ("project_id", "client_email", "private_key")


class PushSettings(BaseSettings):
    """Push notifications for inbox items. Off while `credentials_json` is empty: a deployment turns
    push on by setting the key, and no boot requires it."""

    model_config = cfg("PRISMOID_PUSH_")

    # The Firebase service account's JSON key file, whole. Its `project_id` names the FCM project.
    credentials_json: SecretStr = SecretStr("")
    # FCM's HTTP v1 send endpoint; `{project_id}` is filled from the service account.
    fcm_url: str = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    # The OAuth scope the service account's access token is minted for.
    oauth_scope: str = "https://www.googleapis.com/auth/firebase.messaging"
    timeout_s: float = 10.0
    # How long an inbox item's alert waits before it is sent. A person watching the task reads the
    # item on the page well inside this (the page marks it read about a second after it lands),
    # and an item read by send time rings nothing. 0 sends at once.
    alert_delay_s: float = 5.0

    def enabled(self) -> bool:
        return bool(self.credentials_json.get_secret_value().strip())

    def service_account(self) -> dict[str, Any] | None:
        """The parsed service account, or None while push is off."""
        raw = self.credentials_json.get_secret_value().strip()
        return json.loads(raw) if raw else None

    @model_validator(mode="after")
    def _a_service_account(self) -> PushSettings:
        """A set value must be a service account key: a typo fails the boot here, naming the
        setting and never echoing it, instead of failing every push later."""
        raw = self.credentials_json.get_secret_value().strip()
        if not raw:
            return self
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            parsed = None
        account = cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}
        if any(not account.get(key) for key in _SERVICE_ACCOUNT_KEYS):
            raise ValueError(
                "PRISMOID_PUSH_CREDENTIALS_JSON is not a Firebase service account key "
                "(JSON with project_id, client_email and private_key)"
            )
        return self
