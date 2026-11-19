# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The workspaces group: the tenant knobs.

`prismoid.conf` composes this into `settings` (`settings.workspaces`) and every caller reads it there.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings

from prismoid.conf_env import cfg


class WorkspacesSettings(BaseSettings):
    """Workspaces. Env names are pinned flat: the tenant knobs read as
    PRISMOID_DEFAULT_WORKSPACE_NAME / PRISMOID_TASK_DEFAULT_BUDGET_USD, not under a group prefix."""

    model_config = cfg("PRISMOID_WORKSPACE_")

    invite_ttl_days: int = Field(default=7, validation_alias="PRISMOID_WORKSPACE_INVITE_TTL_DAYS")
    project_invite_ttl_days: int = Field(default=7, validation_alias="PRISMOID_PROJECT_INVITE_TTL_DAYS")
    # Per person, durable: an invite is mail from our domain carrying names the sender chose, so open
    # signup must not make it a free mail cannon. Generous enough to bring a whole team in at once.
    invite_max_per_hour: int = Field(
        default=30, ge=1, validation_alias="PRISMOID_WORKSPACE_INVITE_MAX_PER_HOUR"
    )
    # Per person, fixed window (operations/throttle).
    create_rate_limit_per_minute: int = Field(
        default=5, validation_alias="PRISMOID_WORKSPACE_CREATE_RATE_LIMIT_PER_MINUTE"
    )
    max_founded: int | None = Field(default=None, ge=0, validation_alias="PRISMOID_WORKSPACE_MAX_FOUNDED")
    default_workspace_name: str = Field(default="Default", validation_alias="PRISMOID_DEFAULT_WORKSPACE_NAME")
    # The cap a task created without one gets (ADR-087 section 1); the person can raise it on the task.
    # Unset leaves such a task uncapped.
    task_default_budget_usd: Decimal | None = Field(
        default=None, gt=0, validation_alias="PRISMOID_TASK_DEFAULT_BUDGET_USD"
    )
