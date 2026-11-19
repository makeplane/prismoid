# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The draft scanner - the streaming answer, recovered from inside a tool call.

A writer composes its document as the `content` argument of `submit_document` (a file write's too), so the answer the
person should be watching is born inside tool-call argument JSON. The scanner keeps the raw
argument fragments and reads them with the parser pydantic-ai validates streamed output with
(`from_json`, `allow_partial="trailing-strings"`): the open `content` string decodes as far as
it has arrived, and a half-received escape waits for its tail. Only the new suffix is handed
to the tap. A parse reads the whole buffer, so it runs at most once per `PARSE_EVERY_S` (four
live updates a second) and once more when the call's stream ends (`finish`). A `content` that
is not a string, or arguments that are not JSON, stream nothing; the durable path still tells the story.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, cast

from pydantic_core import from_json

PARSE_EVERY_S: Final = 0.25


@dataclass
class DraftScanner:
    """Feed raw JSON fragments; harvest newly decoded `content` characters and the finished `path`."""

    path: str | None = None
    _raw: str = field(default="", repr=False)
    _sent: int = field(default=0, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _parsed: int = field(default=0, repr=False)
    _parsed_at: float = field(default=-math.inf, repr=False)

    def feed(self, fragment: str) -> str:
        """Take a fragment; the newly decoded `content` when `PARSE_EVERY_S` has passed since the last parse."""
        self._raw += fragment
        return self.finish() if self.clock() - self._parsed_at >= PARSE_EVERY_S else ""

    def finish(self) -> str:
        """Parse everything received so far: the `content` characters not yet handed out."""
        if self._parsed == len(self._raw):
            return ""
        self._parsed, self._parsed_at = len(self._raw), self.clock()
        try:
            args = from_json(self._raw, allow_partial="trailing-strings")
            if self.path is None:
                finished = from_json(self._raw, allow_partial=True)  # drops a string still arriving
                if isinstance(finished, dict) and isinstance(finished.get("path"), str):
                    self.path = finished["path"]
        except ValueError:  # fail-soft: arguments that are not JSON stream no draft
            return ""
        content = cast("dict[str, object]", args).get("content") if isinstance(args, dict) else None
        if not isinstance(content, str) or len(content) <= self._sent:
            return ""
        fresh, self._sent = content[self._sent :], len(content)
        return fresh
