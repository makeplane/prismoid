# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The connector catalog and the suggestion grammar (ADR-054).

The catalog is DATA validated at import - these tests are the landing rule's static half (the
live half is `just connectors-catalog-probe`). The suggestion matcher is the ONE grammar both
the composer chip and `create_task`'s auto-attach speak, so its word-boundary laws are pinned
here once for both consumers.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prismoid.schemas.connector_catalog import (
    CATALOG,
    SHELVED,
    CatalogAuth,
    CatalogEntry,
    entry_for,
    suggest,
    vocabulary,
)

# ── the data itself ───────────────────────────────────────────────────────────────────────────


def test_every_entry_validates_and_slugs_are_unique() -> None:
    slugs = [e.slug for e in CATALOG]
    assert len(slugs) == len(set(slugs)), "two entries with one slug means one is unreachable"
    for e in CATALOG:
        assert e.server_url.startswith("https://")
        assert e.name and e.description and e.category


# The servers that offer no client registration: connecting needs the person's own OAuth client,
# and the web cannot take one yet, so a card would sign nobody in.
NEEDS_OWN_CLIENT = {
    "gmail",
    "google_calendar",
    "google_drive",
    "google_docs",
    "google_sheets",
    "google_slides",
    "google_chat",
    "box",
    "hubspot",
    "github",
    "asana",  # its current server (v2); the old one spoke only the legacy transport
}
# Registration not proven for Prismoid: Stripe registers public clients only, Figma may accept only
# the apps it approved.
UNPROVEN = {"stripe", "figma"}


def test_a_server_that_needs_your_own_client_is_offered_nowhere() -> None:
    offered = {e.slug for e in CATALOG}
    assert offered.isdisjoint(NEEDS_OWN_CLIENT | UNPROVEN)
    assert {e.slug for e in SHELVED} == NEEDS_OWN_CLIENT | UNPROVEN
    for slug in NEEDS_OWN_CLIENT | UNPROVEN:
        assert entry_for(slug) is None, f"{slug} must not resolve to a card, a gate or a bot's offer"


def test_no_server_sits_at_a_legacy_sse_address() -> None:
    # Prismoid's MCP client speaks Streamable HTTP only. An `/sse` address answers the anonymous probe
    # with the same 401 as any server, so the probe passes and every call after sign-in fails.
    for e in (*CATALOG, *SHELVED):
        assert not e.server_url.rstrip("/").endswith("/sse"), f"{e.slug} is at a legacy SSE address"


def test_a_row_made_from_a_shelved_entry_keeps_its_words() -> None:
    words = vocabulary(slug="gmail", name="Gmail", catalog_slug="gmail", tool_names=())
    assert "inbox" in words, "a connector already made from the entry still auto-attaches on its words"


def test_a_catalog_entry_refuses_bad_data_at_import_shape() -> None:
    with pytest.raises(ValidationError, match="https"):
        CatalogEntry(
            slug="bad",
            name="Bad",
            vendor="x",
            description="x",
            category="x",
            server_url="http://insecure.example/mcp",
            auth=CatalogAuth.OPEN,
        )
    with pytest.raises(ValidationError, match="lowercase"):
        CatalogEntry(
            slug="bad",
            name="Bad",
            vendor="x",
            description="x",
            category="x",
            server_url="https://ok.example/mcp",
            auth=CatalogAuth.OPEN,
            keywords=("OK",),
        )
    with pytest.raises(ValidationError, match="slug"):
        CatalogEntry(
            slug="Not-A-Slug",
            name="Bad",
            vendor="x",
            description="x",
            category="x",
            server_url="https://ok.example/mcp",
            auth=CatalogAuth.OPEN,
        )


def test_entry_for_is_case_insensitive_and_honest_about_misses() -> None:
    assert entry_for("Notion") is not None
    assert entry_for(" notion ") is not None
    assert entry_for("nonexistent") is None


# ── the suggestion grammar ────────────────────────────────────────────────────────────────────


def test_word_boundary_only_a_substring_never_fires() -> None:
    # "linear" must not fire on "nonlinear", nor "notion" on "notions of grandeur"... the
    # trailing-s case IS a miss by this grammar (boundary = non-alphanumeric), stated here.
    hits = suggest("a nonlinear equation", [("linear", ("linear",))])
    assert hits == []
    assert suggest("open linear and check", [("linear", ("linear",))]) == [("linear", "linear")]


def test_phrases_match_as_phrases_and_the_longest_keyword_wins() -> None:
    hits = suggest(
        "review the pull request please",
        [("github", ("github", "pull request", "pull"))],
    )
    assert hits == [("github", "pull request")], "provenance names the REAL reason"


def test_short_keywords_never_match() -> None:
    assert suggest("go to it now", [("x", ("it", "go"))]) == []


def test_candidate_order_is_preserved_and_one_hit_per_candidate() -> None:
    cands = [("a", ("alpha",)), ("b", ("beta", "alpha")), ("c", ("gamma",))]
    assert suggest("alpha beta", cands) == [("a", "alpha"), ("b", "alpha")]


def test_tool_name_tokens_make_email_findable_without_a_catalog_entry() -> None:
    # The user's own case: no Gmail catalog entry exists, but a custom
    # connector exposing send_email matches "email this to me" through its tool tokens - the
    # candidate builder feeds those in (operations); the grammar just has to match the word.
    hits = suggest("email this to me when done", [("gmail", ("gmail", "email", "send email"))])
    assert hits == [("gmail", "email")]


def test_case_insensitive_both_sides() -> None:
    assert suggest("Open GitHub please", [("github", ("github",))]) == [("github", "github")]
