# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The note a continued attempt reads: why the last one stopped. `operations/execution` owns the retry ladder."""

from __future__ import annotations

# A reason is a diagnosis, not a transcript: a FallbackExceptionGroup's three provider errors, or
# a request-ceiling message, fit; a runaway exception string is clipped.
CONTINUE_REASON_MAX = 600

# How every continued attempt's note opens.
CONTINUE_PREFIX = "Your previous attempt stopped:"
_CONTINUE = "continue from where you stopped"  # said once, by the note itself


def continue_prompt(reason: str) -> str:
    """The next user turn of a continued run: only why the last attempt stopped, and that what an
    action already did may stand (retry `observe`, never `act`)."""
    why = " ".join(reason.split())[:CONTINUE_REASON_MAX].rstrip(". ")
    if why.lower().endswith(_CONTINUE):  # the request ceiling's own reason says it already
        why = why[: -len(_CONTINUE)].rstrip(";,.- ")
    why = why or "it ended before it finished"
    return (
        f"{CONTINUE_PREFIX} {why}. Continue from where you stopped; do not redo "
        "actions that already took effect. Check the page or files first if unsure."
    )
