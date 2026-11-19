# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""COMPUTER_USE is one enum value plus catalog rows and a routing chain: Sonnet-class
only, the Anthropic row leads, the OpenAI row is the cross-family failover, no Opus row in the
chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import CLASS_TIMEOUT_S
from prismoid.ai.gateway.resolve import chain_for, served_here
from prismoid.schemas.model_class import Family, ModelClass

YAML = Path("prismoid/runner/default_models.yaml")


def _rows() -> list[dict[str, Any]]:
    return yaml.safe_load(YAML.read_text())["models"]


def test_the_class_exists_and_has_a_timeout() -> None:
    assert ModelClass.COMPUTER_USE.value == "computer-use"
    assert CLASS_TIMEOUT_S[ModelClass.COMPUTER_USE] == 180.0


def test_the_shipped_chain_is_sonnet_class_across_two_families() -> None:
    rows = [r for r in _rows() if "computer-use" in r["classes"] and r.get("enabled", True)]
    families = [r["family"] for r in rows]
    assert families[0] == "anthropic", "the Anthropic Sonnet-class row leads"
    assert len(set(families)) >= 2, "a second family is the cross-family failover"
    assert not any("opus" in r["model_id"] for r in rows), "no Opus row in the computer-use chain"
    for row in rows:
        assert row["prices"]["input_token"] <= 0.000003, f"{row['model_id']} is not Sonnet-priced"


def test_the_starter_catalog_ships_opus_and_sonnet_5_5_with_code_on_sonnet() -> None:
    """B5 (owner): Opus 4.8 and Sonnet 5 give way to their 5.5 successors, and the
    `code` class moves to Sonnet 5.5 (roughly half the cost of coding work)."""
    rows = {r["model_id"]: r for r in yaml.safe_load(YAML.read_text())["models"]}
    assert "claude-opus-4-8" not in rows and "claude-sonnet-5" not in rows
    opus, sonnet = rows["claude-opus-5-5"], rows["claude-sonnet-5-5"]
    assert opus["classes"] == ["frontier-reasoner", "judge", "long-context", "vision"]
    assert sonnet["classes"] == ["fast-executor", "frontier-executor", "code", "vision", "computer-use"]
    for row in (opus, sonnet):
        p = row["prices"]
        assert 0 < p["cache_read_token"] < p["input_token"] < p["output_token"], row["model_id"]
    assert opus["context"] == sonnet["context"] == 1000000


def test_the_seeded_chain_leads_with_sonnet_then_the_openai_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alphabetical order put Gemini Flash second, and a fallback chain switched to it mid-run; the
    seeded chain names the failover. (A seat is a row this host can build, so the host here holds
    every provider's key.)"""
    from prismoid.ai.gateway import providers

    monkeypatch.setattr(providers, "api_key_for", lambda provider: "k")
    data = yaml.safe_load(YAML.read_text())
    catalog = [
        ModelSpec(
            provider=r["provider"],
            model_id=r["model_id"],
            family=Family(r["family"]),
            protocol=r["protocol"],
            classes=tuple(ModelClass(c) for c in r["classes"]),
        )
        for r in data["models"]
        if r.get("enabled", True)
    ]
    chain = chain_for(ModelClass.COMPUTER_USE, catalog, order=data["routing"]["computer-use"])
    assert [spec.ref for spec in chain[:2]] == ["anthropic:claude-sonnet-5-5", "openai:gpt-6-sol"]
    assert served_here(ModelClass.COMPUTER_USE, catalog)
