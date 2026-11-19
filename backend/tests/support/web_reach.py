# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a test of the planner's road to the web stands on: the roster with or without the computer,
a catalog row that seats it, a configured search engine, and one agent's entry cut out of a brief.
A plain module, so a test imports it and never another test module."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from prismoid.ai.agents.base import AgentDef, roster
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.conf import settings
from prismoid.schemas.model_class import Family, ModelClass


def row(*classes: ModelClass) -> ModelSpec:
    """A catalog row serving `classes`, built from the worker's anthropic key."""
    return ModelSpec(
        provider="anthropic", model_id="m", family=Family("anthropic"), protocol="anthropic", classes=classes
    )


# A catalog in which the `operator`'s browser seat can run (given the key).
SEATED = [row(ModelClass.COMPUTER_USE)]


def plannable(*, computer: bool = True) -> list[AgentDef]:
    """The planner's roster, with its browsing agent taken out when `computer` is False."""
    return [a for a in roster(plannable=True) if computer or not a.browses]


def an_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A search engine this deployment can call: brave, with its key."""
    monkeypatch.setattr(settings.search, "engines", "brave")
    monkeypatch.setattr(settings.search, "keys", SecretStr("brave=k"))


def entry(brief: str, agent: str) -> str:
    """One agent's roster entry, cut out of a planner brief."""
    start = brief.index(f"\n## {agent}\n")
    end = brief.find("\n## ", start + 1)
    return brief[start : end if end != -1 else len(brief)]
