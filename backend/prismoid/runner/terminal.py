# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a server's or a model's words may put on YOUR terminal. One home, so the stream, the
question prompts and every command that echoes remote text strip escapes the same way."""

from __future__ import annotations


def printable(text: str) -> str:
    """Text as it may reach YOUR terminal: only printable characters. C0/C1 controls (ESC, so no
    CSI or OSC sequence survives), DEL, and the format controls that reorder or hide text (bidi
    overrides, isolates, zero-width marks) all fail `isprintable` and are dropped. Whitespace -
    a line or tab break, a no-break or em space, a line separator - reads as a plain space, so
    one event stays one line and the words either side stay two words."""
    return "".join(ch if ch.isprintable() else " " if ch.isspace() else "" for ch in text)
