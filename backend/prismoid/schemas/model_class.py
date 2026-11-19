# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Model classes - the only model vocabulary application code is allowed to speak.

The asymmetry here is deliberate and is the whole point:

* a **class** is code. Adding one changes what agents can ask for, so it is a deploy.
* a **family** is data. `xai` ships `grok-5-fast` on a Tuesday; if `Family` were an enum, that
  Tuesday would need a release. It is a string, and the catalog row is the only place it lives.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, NewType

# "anthropic" | "openai" | "google" | "xai" | "deepseek" | "local" | whatever ships next week.
# Cross-family verification compares these; `exclude_family` on an AgentDef excludes
# by this value. Never an enum - see the docstring.
Family = NewType("Family", str)

# The wire protocol a provider speaks. THIS is closed: a genuinely novel SDK is a code change:
# one adapter in ai/gateway/providers.py, a dependency row, and an ADR. The two `*_IMAGES`
# members are MEDIA protocols: their rows live in the same catalog (priced, enabled,
# routed like any row) but are served by the media TOOL (`ai/tools/providers/images.py`), never
# by a chat constructor - `build_model` refuses them (its `FEATURES` table) so a mistaken fleet pin to an image
# model is an honest error instead of a confusing SDK crash.
Protocol = StrEnum(
    "Protocol",
    {
        "ANTHROPIC": "anthropic",
        "OPENAI": "openai",
        "GOOGLE": "google",
        "OPENAI_COMPATIBLE": "openai_compatible",  # DeepSeek, xAI, Ollama, vLLM, most startups
        "OPENAI_IMAGES": "openai_images",  # the OpenAI Images wire - OpenAI itself, and xAI's grok-imagine via base_url
        "GOOGLE_IMAGES": "google_images",  # Imagen via google-genai
    },
)


class ModelClass(StrEnum):
    """What a step needs from a model, said without naming one."""

    FAST_EXECUTOR = "fast-executor"  # the default worker: quick, cheap, competent
    FRONTIER_EXECUTOR = "frontier-executor"  # tool-using work at frontier tier, every fallback the same tier
    FRONTIER_REASONER = "frontier-reasoner"  # planning, synthesis, hard judgement
    LONG_CONTEXT = "long-context"  # book-length inputs (the endurance case)
    CHEAP_CLASSIFIER = "cheap-classifier"  # routing, triage, extraction
    CODE = "code"  # writes and repairs programs
    VISION = "vision"  # reads images, decks, scanned PDFs
    JUDGE = "judge"  # the cross-family verifier - never the producer's family
    IMAGE_GEN = "image-gen"  # text → image, served by the media tool - never a chat seat
    COMPUTER_USE = "computer-use"  # strong structured tool use + vision; the operator's seat
    DECIDER = "decider"  # one cheap decision run: whichever decision model the deployment has a key for


# A class a host may hold no buildable row for, and the class it then runs on: a host without an
# Anthropic or OpenAI key, or a catalog not yet re-seeded, keeps its Lead on the class it had before.
DEGRADES_TO: Final = {ModelClass.FRONTIER_EXECUTOR: ModelClass.FAST_EXECUTOR}


# The classes whose models can author a deliverable, so each may hold a fleet seat: a judge-only row
# grades and a vision-only row reads, and neither writes the person a document.
EXECUTOR_CLASSES: Final = (
    ModelClass.FRONTIER_REASONER,
    ModelClass.FAST_EXECUTOR,
    ModelClass.FRONTIER_EXECUTOR,
    ModelClass.LONG_CONTEXT,
    ModelClass.CODE,
)


class JudgeFamily(StrEnum):
    """How a judge stands to the maker - the words a report prints, nearest the maker last."""

    OTHER = "other family"  # the judge is outside the maker's family
    SAME = "same family"  # another model of the maker's family
    SELF = "self-checked"  # the maker's own model, in a fresh context
    NONE = "no judge"  # no judge row can be built on this host

    @classmethod
    def between(cls, *, maker: str, maker_family: Family, judge: str, judge_family: Family) -> JudgeFamily:
        """How one judge model stands to the maker; both models are `provider:model_id` refs."""
        if judge_family != maker_family:
            return cls.OTHER
        return cls.SELF if judge == maker else cls.SAME
