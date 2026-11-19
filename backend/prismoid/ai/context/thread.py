# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The thread transcript as a model reads it: instruction, never evidence (same framing as `blocks.py`).
Append-only, so under budget turn K's render is a byte-prefix of turn K+1's and the prompt cache holds."""

from __future__ import annotations

from prismoid.schemas.thread import ThreadEntry, ThreadTranscript, ThreadTurn

_REPLY = "**Prismoid replied:**"
_NAME_MAX = 60
_FALLBACK = "The user"
_STRIP = str.maketrans("", "", "*_`#<>[]|\\")


def safe_name(raw: str) -> str:
    """A display name is user-controlled DATA: one line, no markup, capped."""
    one_line = " ".join(raw.translate(_STRIP).split())
    return one_line[:_NAME_MAX].rstrip() or _FALLBACK


def author_line(transcript: ThreadTranscript) -> str | None:
    """The Lead's one line about who is asking now (the name sanitized and quoted as data, never a
    greeting cue), or None when no human is named or the name sanitizes away."""
    if not transcript.current_author:
        return None
    name = safe_name(transcript.current_author.replace('"', ""))
    if name == _FALLBACK:
        return None
    return f'The person asking now is "{name}".'


def _who(entry: ThreadEntry) -> str:
    return safe_name(entry.author.name) if entry.author is not None else _FALLBACK


def _entry_md(entry: ThreadEntry) -> str:
    if entry.kind == "user":
        return f"**{_who(entry)} asked:**\n\n{entry.text}"
    if entry.kind == "steering":
        return f"**{_who(entry)} steered:** {entry.text}"
    if entry.kind == "assistant":
        return f"{_REPLY}\n\n{entry.text}"
    if entry.kind == "qa":
        return entry.text  # pre-joined "**Asked:** … **Cy answered:** …" ("They said" if unnamed)
    return f"*{entry.text}*"  # event markers arrive pre-bracketed "[…]"


def _blocks(transcript: ThreadTranscript) -> str:
    """Turn blocks, display-numbered across the whole chain (a fork's lineage reads as one
    conversation), with the honest elision line after the anchor when the budget bit."""
    parts: list[str] = []
    number = 0
    for i, turn in enumerate(transcript.turns):
        if turn.turn >= 1:
            number += 1
        parts.append(_turn_block(turn, number))
        if i == 0 and transcript.elided_turns:
            parts.append(
                f"*[{transcript.elided_turns} earlier turn(s) elided - "
                "the full conversation is in the task record]*"
            )
    return "\n\n".join(parts)


def _turn_block(turn: ThreadTurn, number: int) -> str:
    body = "\n\n".join(_entry_md(e) for e in turn.entries)
    if turn.turn == 0:
        # The conversation a chat-born task came from: the talk before turn 1,
        # framed as what it is - the same instruction-not-source wall as every other line.
        return f"## Before this task - the conversation it came from\n\n{body}"
    return f"## Turn {number}\n\n{body}"


def render_thread_md(transcript: ThreadTranscript) -> str:
    """The `/task/thread.md` body - mounted read-only beside the sources, framed apart from them."""
    head = (
        "# The conversation so far\n\n"
        "**This file is the conversation that led to this task - instruction and context, not a "
        'source.** Read it so "that memo", "the same format" and "like last time" mean what '
        "the user means. You never cite it, quote it as evidence, or list it as provenance; "
        "everything you assert still comes from the sources."
    )
    return "\n\n".join([head, _blocks(transcript)]) + "\n"
