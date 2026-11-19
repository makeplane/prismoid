# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""SCREEN 2 - every agent in the product. One import per agent, and that is the whole file.

Read it top to bottom and you know the roster. An agent that is not imported here does not exist:
it is not in `GET /v1/agents`, not in the Lead's roster, and not in the parametrized CI
test that runs one real case against every registered agent.

Importing this module is what populates `REGISTRY` - so `api`, `jobs` and the worker all import
it exactly once, at boot.
"""

from __future__ import annotations

from prismoid.ai.agents import bot_lesson as bot_lesson  # the import IS the registration
from prismoid.ai.agents import claim_extract as claim_extract
from prismoid.ai.agents import judge as judge
from prismoid.ai.agents import lead as lead
from prismoid.ai.agents import maker as maker
from prismoid.ai.agents import memory_extract as memory_extract
from prismoid.ai.agents import namer as namer
from prismoid.ai.agents import operator as operator
from prismoid.ai.agents import page_reader as page_reader
from prismoid.ai.agents import researcher as researcher
from prismoid.ai.agents import summarizer as summarizer
from prismoid.ai.agents import writer as writer
from prismoid.ai.agents.base import REGISTRY, AgentDef, get, roster

__all__ = [
    "REGISTRY",
    "AgentDef",
    "bot_lesson",
    "claim_extract",
    "get",
    "judge",
    "lead",
    "maker",
    "memory_extract",
    "namer",
    "operator",
    "page_reader",
    "researcher",
    "roster",
    "summarizer",
    "writer",
]
