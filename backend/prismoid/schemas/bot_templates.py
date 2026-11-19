# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bot template shapes (ADR-056) - a bot, published as an immutable snapshot that travels
as a link.

The template is the anti-Grok share: only a PUBLISHED head can become one (so every shared bot
earned green selection evals once), the snapshot never tracks the author's later edits, the
preview shows EVERYTHING that installs (full instructions included - transparency is the
anti-smuggling courtesy), and credentials are structurally absent (connector rows are a
different table; only slugs travel).

The secret scanner here is a NET, NOT A PROOF, and the ADR says so: the load-bearing layers
are the full-transparency preview and the installs-as-a-draft posture. The scanner exists
because the one Grok lapse everyone hit first was keys pasted into a description travelling
with the link.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class TemplateStatus(StrEnum):
    PUBLISHED = "published"  # the anon preview serves it
    UNLISTED = "unlisted"  # the author unpublished: the link 404s, the rows stay
    REMOVED = "removed"  # the operator's one audited verb (admin unlist)


class TemplateVisibility(StrEnum):
    UNLISTED = "unlisted"  # link-only, v1's whole surface
    LISTED = "listed"  # the future gallery's WHERE clause - shipped so it is never a migration


MAX_STARTER_MEMORIES: Final = 10  # deferred lane: ships with the memory pillar

# The credential net: house prefixes and the common vendor key shapes. Each entry is
# (name, compiled pattern) so a refusal can say WHICH shape it saw, not just "a secret".
SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("a Plane session token", re.compile(r"\bcs_[A-Za-z0-9_-]{20,}")),
    ("a Plane API key", re.compile(r"\bck_[A-Za-z0-9_-]{20,}")),
    ("a Plane reset code", re.compile(r"\bcr_[A-Za-z0-9_-]{20,}")),
    ("a connector state token", re.compile(r"\bcn_[A-Za-z0-9_-]{20,}")),
    ("a connector return code", re.compile(r"\bca_[A-Za-z0-9_-]{20,}")),
    ("a routine fire token", re.compile(r"\brt_[A-Za-z0-9_-]{20,}")),
    ("Fernet ciphertext", re.compile(r"\bgAAAA[A-Za-z0-9_-]{20,}")),
    ("an OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("a Slack bot token", re.compile(r"\bxox[abps]-[A-Za-z0-9-]{10,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

_URL = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)


def scan_for_secrets(text: str) -> list[str]:
    """Every credential shape found, named. Empty = the net caught nothing (not proof of none)."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def external_urls(text: str) -> list[str]:
    """Every absolute URL in the text, deduped in order - publish surfaces these for the author
    to acknowledge (bots legitimately cite docs; the preview renders them as plain text)."""
    seen: dict[str, None] = {}
    for m in _URL.findall(text):
        seen.setdefault(m.rstrip(".,;"), None)
    return list(seen)


class InstallNote(BaseModel):
    """One line of the install receipt: a skill that did not resolve, a connector to connect."""

    model_config = ConfigDict(frozen=True)

    kind: str  # skill_missing | needs_connect | note
    detail: str


class TemplateInstallOutcome(BaseModel):
    """What one click produced: the draft's name and version, and the honest notes."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: int
    notes: tuple[InstallNote, ...] = ()
    needs_connect: tuple[str, ...] = ()  # catalog slugs the installer has not connected


class TemplatePublishBlock(BaseModel):
    """Why a publish was refused, typed: the scanner's hits or the unacknowledged URLs."""

    model_config = ConfigDict(frozen=True)

    secrets: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()


MAX_BYLINE_CHARS: Final = 120
MAX_TEMPLATE_VERSIONS: Final = 100  # a runaway republish loop backstop, far above real use


class TemplateRef(BaseModel):
    """slug@version - the provenance string an installed bot's `source` carries."""

    model_config = ConfigDict(frozen=True)

    slug: str = Field(min_length=1, max_length=48)
    version: int = 1

    def source(self) -> str:
        return f"template:{self.slug}@v{self.version}"
