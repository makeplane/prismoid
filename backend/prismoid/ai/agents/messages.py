# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A run's message history as a value: `continue_with` adds a user line to run again.

A pure function over pydantic-ai's message types, no agent and no registry. `base.py` re-exports it
for its callers."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart


def continue_with(history: Sequence[ModelMessage], text: str) -> list[ModelMessage]:
    """A finished or stopped run's history with one more user line (appended to a trailing request, else
    a new one), to run again with no prompt; the saved history is never mutated."""
    messages = list(history)
    line = UserPromptPart(text)
    last = messages[-1] if messages else None
    if isinstance(last, ModelRequest):
        messages[-1] = dataclasses.replace(last, parts=[*last.parts, line])
    else:
        messages.append(ModelRequest(parts=[line]))
    return messages
