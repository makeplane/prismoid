# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Whether a task's creator can still act: the one vocabulary the worker's doors share."""

from __future__ import annotations

from enum import StrEnum


class Standing(StrEnum):
    ACTIVE = "active"  # an active account that still belongs to the task's workspace
    SUSPENDED = "suspended"  # the account is missing or not active
    LEFT = "left"  # the account is fine, but they hold no membership in the task's workspace
