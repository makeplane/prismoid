# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The one token estimate: about four characters a token.

A guard-rail, never a bill: it sizes a skill's body, a bot's instructions, the thread and the
context a step mounts, and a run's history against its model's window. A real tokenizer per
provider would cost more than the precision is worth, and every reader needs the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class TokenCounter:
    """Tokens estimated from characters, rounded up."""

    chars_per_token: int = 4

    def count(self, text: str) -> int:
        """The estimated tokens in `text`."""
        return -(-len(text) // self.chars_per_token)


TOKENS: Final = TokenCounter()
