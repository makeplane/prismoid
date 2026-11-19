# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every brief carries the ambient facts: a cloud Machine cannot reach the person's own computer or
network (a browser opened for "check http://localhost:8000/admin" can only fail), and today where
the person is. One line each, the stable fact first, so the cached prefix holds up to the date."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from prismoid.ai.context.blocks import MACHINE_REACH, today_section
from prismoid.ai.context.brief import prompt_for
from prismoid.operations.execution.express import ambient_facts, express_prompt
from prismoid.schemas.connectors import ServiceReach
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName, TaskId
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.thread import ThreadEntry, ThreadTranscript, ThreadTurn

NOW = dt.datetime(2026, 11, 12, 20, 30, tzinfo=dt.UTC)
ASK = "check http://localhost:8000/admin"


def test_the_facts_are_one_line_each_the_stable_one_first() -> None:
    facts = [line for line in today_section("Asia/Kolkata", NOW).splitlines() if line.startswith("- ")]
    assert facts[0] == MACHINE_REACH and len(facts) == 3
    assert "Tele-MANAS: 14416" in facts[1]
    assert facts[2].startswith("- Today is Friday 2026-11-13 where the user is (Asia/Kolkata), in India.")
    for unreachable in ("localhost", "private addresses", "192.168.x", ".local"):
        assert unreachable in MACHINE_REACH


def test_only_the_date_moves_with_the_clock() -> None:
    today, later = today_section("UTC", NOW), today_section("UTC", NOW + dt.timedelta(days=3))
    head = today.split("- Today is")[0]
    assert today != later and later.startswith(head) and MACHINE_REACH in head


def test_a_localhost_ask_reaches_the_step_and_the_lead_with_the_fact() -> None:
    facts = today_section("UTC", NOW)
    step = prompt_for(
        StepSpec(agent=AgentName("operator"), title="Check", contract=Contract(goal=ASK)), today=facts
    )
    assert step.endswith(facts), "per-turn text last, after the stable brief"
    assert MACHINE_REACH in ambient_facts("UTC", NOW, computer=True), (
        "the Lead that would plan a browser step"
    )


@pytest.mark.parametrize(("tz", "said"), [("Asia/Kolkata", "Asia/Kolkata"), (None, "UTC"), ("", "UTC")])
def test_the_ambient_clock_is_the_askers(tz: str | None, said: str) -> None:
    facts = ambient_facts(tz, NOW)
    assert facts.startswith(
        f"# Ambient facts\n\n- current UTC time: 2026-11-12 20:30 UTC\n- the user's timezone: {said}\n"
    )


@pytest.mark.parametrize(
    ("services", "line"),
    [
        ((), "- services connected to this task: none"),
        (("Gmail", "Linear"), "- services connected to this task: Gmail, Linear"),
    ],
)
def test_the_facts_say_which_services_the_turn_can_reach(services: tuple[str, ...], line: str) -> None:
    """A question like "When did I last email Sarah?" with no mail connected must not be planned and
    answered by a writer explaining it cannot see the mail: the Lead is told what it can reach."""
    facts = ambient_facts("UTC", NOW, services=services)
    assert facts.startswith(ambient_facts("UTC", NOW)) and facts.endswith(f"{line}\n")


def test_a_bare_prompt_is_the_steps_own_then_the_facts() -> None:
    assert express_prompt("ASK", ThreadTranscript(), ambient="FACTS") == "ASK\n\nFACTS"


def test_the_prompt_reads_thread_ask_asker_then_facts() -> None:
    task = TaskId(uuid.UUID(int=1))
    ask = ThreadEntry(kind="user", text="what time is it?", task_id=task, turn=1, seq=1, tokens=4)
    reply = ThreadEntry(kind="assistant", text="Half nine.", task_id=task, turn=1, seq=2, tokens=3)
    transcript = ThreadTranscript(
        turns=[ThreadTurn(task_id=task, turn=1, entries=[ask, reply])], current_author="Ada"
    )
    prompt = express_prompt("ASK", transcript, ambient="FACTS")
    heads = ["# The conversation so far", "ASK", "# Who is asking", "FACTS"]
    places = [prompt.index(h) for h in heads]
    assert places == sorted(places) and prompt.endswith("\n\nFACTS")
    assert "what time is it?" in prompt and "Ada" in prompt


def test_the_facts_name_what_a_step_can_do_beyond_the_leads_reads() -> None:
    """With send_money attached the Lead never says the bank only reads: the write is a step's to call."""
    reach = (
        ServiceReach(name="Bank", steps_can=("send_money",)),
        ServiceReach(name="Linear", turned_off=("delete_issue",)),
    )
    facts = ambient_facts("UTC", NOW, services=("Bank", "Linear"), reach=reach)
    assert "- Bank: a planned step can also call send_money" in facts
    assert "- Linear: turned off in the person's connector settings: delete_issue\n" in facts
