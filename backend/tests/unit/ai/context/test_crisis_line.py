# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The prompts that answer a person carry the crisis line for where they are.

The step/planner brief (`today_section`) names the country's helpline; the Lead's prompt
(`ambient_facts`, the surface that writes a task thread's reply) carries the country; a bot's
conversation reply (`chat_reply._now`) carries both, and an unknown country gets the directory."""

from __future__ import annotations

import datetime as dt

from prismoid.ai.context.blocks import region, today_section, where_facts
from prismoid.operations.execution.express import ambient_facts

NOW = dt.datetime(2026, 11, 16, 9, 0, tzinfo=dt.UTC)


def test_the_country_is_known_for_an_indian_timezone() -> None:
    assert region("Asia/Kolkata") == "India"  # the fact the fix would key on already exists


def test_the_step_brief_names_indias_crisis_line() -> None:
    assert "14416" in today_section("Asia/Kolkata", NOW)


def test_the_leads_ambient_facts_name_the_country() -> None:
    """The Lead answers a distressed first message in a task thread; it is told only a tz name."""
    assert "India" in ambient_facts("Asia/Kolkata", NOW)


def test_a_bot_reply_hears_the_owners_country_and_a_stranger_gets_the_directory() -> None:
    """`chat_reply._brief` appends `where_facts` for the owner's timezone, None for a stranger."""
    assert "India" in where_facts("Asia/Kolkata") and "14416" in where_facts("Asia/Kolkata")
    assert "findahelpline.com" in where_facts(None) and "988" not in where_facts(None)


def test_an_unknown_country_gets_no_country_specific_number() -> None:
    text = today_section("UTC", NOW)
    assert "988" not in text and "14416" not in text
