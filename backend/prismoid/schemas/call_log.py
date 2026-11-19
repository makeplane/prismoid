# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log's phase: a logged unit's intent (before the call runs) or its result."""

from __future__ import annotations

from enum import StrEnum


class CallPhase(StrEnum):
    INTENT = "intent"
    RESULT = "result"
