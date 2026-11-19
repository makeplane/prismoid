# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""TaskInput - a file the user attached to a task, and the ground truth a verifier checks against.

**This is the other half of verification, and without it verification is theatre.** "Every claim is
traceable to a source" is only a testable sentence if there *are* sources: a memo written from
nothing can be checked against nothing, and a suite that runs zero checks and reports `pass` is the
silent-success failure mode wearing a green tick.

So an input is a first-class value, and it is **content-addressed like an artifact** - the bytes go
to storage under their own SHA-256 and the task row keeps the digest. Three things fall out:

* attaching the same brief to twenty tasks costs one object;
* the frame gets *exactly* the bytes the user sent, verifiably, because the key **is** the hash;
* the verifier re-derives from the same bytes the writer read - not from a copy that may have
  drifted between them.

**This file does not know where a frame puts them.** `schemas` sits below `ai` in the layering law,
so it holds the *filename*; `ai/frames/layout.input_path()` turns that into `/task/inputs/…`.
That is not bureaucracy - it is why a value stays a value: the same `TaskInput` describes a file in
a Docker frame, a gVisor frame, or a row in a test that has no frame at all.

**Every input part is user material, never the person's instruction**: it is read, cited and fenced
as data, and a host or address inside it is never one they named - only the typed message
pre-approves a site. `visible` reads that typed message as the person sees it.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_INPUTS: Final = 10
MAX_INPUT_BYTES: Final = 5 * 1024 * 1024  # base64-in-JSON is honest for a few MB - see the CLI

# A filename, and nothing that could be a path. `../../etc/passwd` is a filename to anyone who does
# not look, and `posixpath.basename()` alone would cheerfully hand back `passwd` - silently writing
# somewhere the caller never named. So: **reject, don't sanitise.** A sanitiser turns an attack into
# a surprise; a validator turns it into a 422 that names the problem.
# What a HUMAN's file is actually called: letters (any script - \w is unicode), digits,
# spaces, and the punctuation real names carry ("CleanShot at 00.14.21@2x.png",
# "résumé.pdf", "my report (final).docx"). Everything shell-dangerous stays banned - / \
# quotes, $, backticks, ;, &, |, <>, *, ? - so a frame path built from this name can never
# escape quoting, and repr() always single-quotes it (single quotes are banned too).
_SAFE_NAME: Final = re.compile(r"^[\w()@+,.\- ]{1,120}$", re.UNICODE)


def a_name_not_a_path(value: str) -> str:
    """The validator both input shapes share. One definition, because two would drift - and the
    one that drifts is the one that stops rejecting `..`. Accepts what humans call files
    (spaces, parentheses, @, accents - a CleanShot name was once a 422); still refuses anything
    that could be an address (separators, traversal), hide (leading dot), flag-inject
    (leading dash), or escape shell quoting."""
    if (
        value != posixpath.basename(value)
        or not _SAFE_NAME.match(value)
        or value.startswith((".", "-", " "))
        or value.endswith(" ")
    ):
        raise ValueError(
            f"{value!r} is not a filename: attachments are named, not addressed - no "
            "directories, no traversal, no leading dot or dash"
        )
    return value


# A native program, by its first bytes - never by the name or the client's media type, which the sender
# chooses. Mach-O and its fat form (which shares 0xCAFEBABE with a Java class file: refused alike).
_NATIVE_MAGIC: Final[dict[bytes, str]] = {
    b"\x7fELF": "a Linux program (ELF)",
    b"\xfe\xed\xfa\xce": "a macOS program (Mach-O)",
    b"\xfe\xed\xfa\xcf": "a macOS program (Mach-O)",
    b"\xce\xfa\xed\xfe": "a macOS program (Mach-O)",
    b"\xcf\xfa\xed\xfe": "a macOS program (Mach-O)",
    b"\xca\xfe\xba\xbe": "a macOS universal program or Java class",
}


def native_program(data: bytes) -> str | None:
    """What kind of native program `data` is, or None. A Windows one is `MZ` whose header points at a `PE`
    signature, so a text file that merely starts with "MZ" is not one."""
    if (kind := _NATIVE_MAGIC.get(data[:4])) is not None:
        return kind
    if data[:2] == b"MZ" and len(data) >= 0x40:
        pe = int.from_bytes(data[0x3C:0x40], "little")
        if data[pe : pe + 4] == b"PE\x00\x00":
            return "a Windows program (.exe or .dll)"
    return None


def not_a_source(data: bytes) -> str | None:
    """Why these bytes cannot be a task's source, or None. An empty file would make every grounding check
    vacuous; a program is not a source, and a task's computer never runs one a person or a sender attached."""
    if not data:
        return "is empty"
    if (kind := native_program(data)) is not None:
        return f"is {kind}; programs can't be attached"
    return None


class RawInput(BaseModel):
    """An attachment **before** it is stored - the bytes are still here.

    Two shapes, deliberately: `operations.create_task` takes this, puts the bytes in the object
    store, and hands back a `TaskInput` that carries only the digest. The row never holds a payload
    and the payload never holds a filename it could lie about.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    data: bytes
    media_type: str = "text/plain"

    @field_validator("filename")
    @classmethod
    def _check(cls, value: str) -> str:
        return a_name_not_a_path(value)


class TaskInput(BaseModel):
    """One attached file, as the task row remembers it. The bytes live in storage, never here."""

    model_config = ConfigDict(frozen=True)

    filename: str
    sha256: str
    bytes: int
    media_type: str = "text/plain"

    @field_validator("filename")
    @classmethod
    def _check(cls, value: str) -> str:
        return a_name_not_a_path(value)

    @property
    def is_text(self) -> bool:
        """Whether the part is text by its media type: `text/*`, or JSON (a routine fire's trigger.json)."""
        media = self.media_type.split(";", 1)[0].strip().lower()
        return media.startswith("text/") or media == "application/json" or media.endswith("+json")


class InputSet(BaseModel):
    """A task's attachments. A value, so `tasks.inputs` (JSONB) stops being JSON at the edge."""

    model_config = ConfigDict(frozen=True)

    items: list[TaskInput] = Field(default_factory=list[TaskInput], max_length=MAX_INPUTS)

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)


# What nobody sees: controls (newline and tab stay), format characters (bidi overrides, zero-widths, the
# BOM) and the tag block - except a zero-width joiner between two symbols, which binds an emoji ("❤️ ZWJ 🔥").
_KEPT_CONTROLS: Final = frozenset("\n\t")
_TAG_BLOCK: Final = range(0xE0000, 0xE0080)
_ZWJ: Final = "\u200d"
_LONE_JOINER: Final = re.compile(r"(?<![^\w\s])\u200d|\u200d(?![^\w\s])")


def _unseen(char: str) -> bool:
    if char in _KEPT_CONTROLS or char == _ZWJ:
        return False
    return unicodedata.category(char) in ("Cc", "Cf") or ord(char) in _TAG_BLOCK


def visible(text: str) -> str:
    """Text as a person sees it, ends stripped: the one invisible-character rule (typed messages, server prose)."""
    return _LONE_JOINER.sub("", "".join(c for c in text if not _unseen(c))).strip()
