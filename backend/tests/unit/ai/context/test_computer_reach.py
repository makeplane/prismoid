# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The models are told the computer exists and what it can do, and every step knows the person's today.

The ambient facts say a computer is there only when it is. Once seated, the computer opens the site before
asking, hands the person their own sign-in and payment, and uses a brand's site for the person's region,
which comes from the system's own tz tables. A step's brief ends on today where the person is, so it reads
"next Friday" against their calendar."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from prismoid.ai.agents.base import browsing, roster
from prismoid.ai.agents.operator import OPERATOR
from prismoid.ai.context.blocks import today_section
from prismoid.operations import planning_brief
from prismoid.operations.execution.express import ambient_facts

NOW = dt.datetime(2026, 11, 12, 20, 30, tzinfo=dt.UTC)
COMPUTER_PROMPT = browsing(OPERATOR, on=True).prompt  # what a step on the browser is told


def _flat(text: str) -> str:
    return " ".join(text.split())


# --- the computer exists only where it does, and the seats say who can ask ---


def test_the_ambient_facts_say_the_computer_exists_only_when_it_does() -> None:
    assert "computer" in ambient_facts("UTC", NOW, computer=True)
    assert "computer" not in ambient_facts("UTC", NOW)


def test_the_roster_says_who_can_ask() -> None:
    plannable = roster(plannable=True)
    assert any(a.can_ask_user for a in plannable)
    for agent in plannable:
        assert ("Can ask the user mid-run." in planning_brief.roster_entry(agent)) is agent.can_ask_user
    assert "sign in or pay" in _flat(OPERATOR.description)


# --- today, where the person is ---


def test_today_is_the_persons_own_date_and_country() -> None:
    today = today_section("Asia/Kolkata", NOW)
    assert today.endswith(
        "\n- Today is Friday 2026-11-13 where the user is (Asia/Kolkata), in India. A relative date "
        'in the request ("next Friday", "tomorrow") counts from it.\n'
    )


def test_an_unknown_zone_is_utc() -> None:
    assert "Thursday 2026-11-12 where the user is (UTC)." in today_section("Mars/Olympus", NOW)
    assert "(UTC)." in today_section(None, NOW)


def test_the_region_is_the_zones_country_and_nothing_when_unknown(tmp_path: Path) -> None:
    """A bare "amazon" must land on the person's own site (the US site serves a bot check to a
    person in India; amazon.in does not). The country comes from the system's own tz tables."""
    from prismoid.ai.context.blocks import region

    (tmp_path / "zone.tab").write_text("# c\nIN\t+2232+08822\tAsia/Kolkata\nDE\t+5230+01322\tEurope/Berlin\n")
    (tmp_path / "iso3166.tab").write_text("# c\nIN\tIndia\nDE\tGermany\n")
    tzpath = (str(tmp_path / "missing"), str(tmp_path))
    assert region("Asia/Kolkata", tzpath) == "India"
    assert region("Europe/Berlin", tzpath) == "Germany"
    assert region("UTC", tzpath) is None and region(None, tzpath) is None
    assert region("Asia/Kolkata", (str(tmp_path / "missing"),)) is None, "no tables, no guess"


def test_a_legacy_zone_name_follows_its_link_to_the_country(tmp_path: Path) -> None:
    """Browsers report the old link names ("Asia/Calcutta", "US/Eastern"), which `zone.tab` never
    lists; the link lines in `tzdata.zi` beside it name the zone each one stands for."""
    from prismoid.ai.context.blocks import region

    (tmp_path / "zone.tab").write_text(
        "# c\nIN\t+2232+08822\tAsia/Kolkata\nUS\t+404251-0740023\tAmerica/New_York\n"
        "HR\t+4548+01558\tEurope/Zagreb\n"
    )
    (tmp_path / "iso3166.tab").write_text("# c\nIN\tIndia\nUS\tUnited States\nHR\tCroatia\n")
    (tmp_path / "tzdata.zi").write_text(
        "# version x\nZ Asia/Kolkata 5:30 - IST\nL Asia/Kolkata Asia/Calcutta\n"
        "L America/New_York US/Eastern\nL US/Eastern Legacy/Twice\nL Europe/Belgrade Europe/Zagreb\n"
    )
    tzpath = (str(tmp_path),)
    assert region("Asia/Calcutta", tzpath) == "India"
    assert region("US/Eastern", tzpath) == "United States"
    assert region("Legacy/Twice", tzpath) == "United States", "a link to a link still lands"
    assert region("Europe/Zagreb", tzpath) == "Croatia", "a zone's own row outranks a link"
    assert region("Nowhere/Linked", tzpath) is None


def test_the_systems_own_tables_know_calcutta() -> None:
    from prismoid.ai.context.blocks import region

    if region("Asia/Kolkata") is None:
        pytest.skip("this system ships no tz tables")
    assert region("Asia/Calcutta") == region("Asia/Kolkata")
    assert ", in India." in today_section("Asia/Calcutta", NOW)


def test_the_computer_uses_a_brands_site_for_the_persons_region() -> None:
    assert "use its site for their region" in _flat(COMPUTER_PROMPT)


# --- the computer: drive first, ask late; an account is signed into, never assumed ---


def test_the_computer_drives_first_and_hands_the_person_their_own_details() -> None:
    told = _flat(COMPUTER_PROMPT)
    for idea in ("Drive first, ask late.", "`browser_handoff`", "go on as a guest", "never accept cookies"):
        assert idea in told, idea


def test_a_step_brief_carries_today_last() -> None:
    """No plan resolves "next Friday" for a step: its own brief says what day it is, after everything stable."""
    from prismoid.ai.context.brief import prompt_for
    from prismoid.schemas.contract import Contract
    from prismoid.schemas.ids import AgentName
    from prismoid.schemas.plan import StepSpec

    spec = StepSpec(
        agent=AgentName("operator"), title="Book", contract=Contract(goal="book it for next Friday")
    )
    today = today_section("UTC", NOW)
    assert prompt_for(spec, today=today).endswith(today)
    assert "# Ambient facts" not in prompt_for(spec)
