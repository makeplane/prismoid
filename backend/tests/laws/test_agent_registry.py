# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""SCREEN 2: the roster, and the invariants that keep it a roster.

The parametrized test over `roster()` is the mechanism that makes it true: *adding an agent = one file +
one prompt + one import in all.py*, and it is then live in the registry, in `GET /v1/agents`, and
**in this test** - which every new agent must pass without anyone remembering to add it here.
"""

from __future__ import annotations

import pytest

from prismoid.ai.agents import grants
from prismoid.ai.agents.all import get, roster
from prismoid.ai.agents.base import IDENTITY, AgentDef, browsing, definition_for, instructions_for
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.ask import CONNECT_GRANT
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ARTIFACT_TYPES


def test_the_writer_is_registered() -> None:
    writer = get("writer")
    assert writer.model_class is ModelClass.FAST_EXECUTOR
    assert writer.can_ask_user  # a top-level step agent


def test_an_unknown_agent_names_the_ones_that_exist() -> None:
    """An error message that lists the alternatives is the difference between a two-minute fix
    and a twenty-minute one - and the list is non-empty because the registry loads itself."""
    with pytest.raises(KeyError, match="writer"):
        get("wrtier")


def test_the_registry_loads_itself_in_a_fresh_process() -> None:
    """`no agent named 'writer'; known: []` - the message was true and useless.

    The bug was in whichever module forgot to import `all.py`, and **the import IS the
    registration**. A registry you can forget to load is a registry that will be forgotten, so it
    loads itself at the first question anyone asks it.

    Tested in a **subprocess**, deliberately. Clearing `REGISTRY` in-process and re-importing
    proves nothing: `all.py` is already in `sys.modules`, so the import is a no-op and the
    registration never re-runs - the test would pass while testing nothing. The only honest test
    of *"a fresh process that imports nothing but `base` can still resolve an agent"* is a fresh
    process.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "from prismoid.ai.agents.base import get; print(get('writer').name)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "writer"


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_every_agent_is_a_role_not_a_model(agent: AgentDef) -> None:
    """Agents are roles ("modeler"), never models. An AgentDef that names a model has
    smuggled a routing decision into the registry, where it cannot be changed without a deploy."""
    banned = ("gpt", "claude", "gemini", "grok", "deepseek", "-4", "-5", "sonnet", "opus", "haiku")
    assert not any(b in str(agent.name).lower() for b in banned)


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_every_agent_declares_what_it_is_for_and_what_it_is_not(agent: AgentDef) -> None:
    """The description is the Planner's routing trigger. "Not for…" is what stops the
    Planner reaching for a writer when it needs a modeler."""
    assert len(agent.description) > 20
    assert "not for" in agent.description.lower()


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_every_agent_has_a_prompt(agent: AgentDef) -> None:
    assert agent.prompt.strip(), f"{agent.name} has no prompt"


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_an_agent_that_can_publish_must_say_how_its_output_is_checked(agent: AgentDef) -> None:
    """The custom-agent doctrine: declare `verifier_suite`. An agent whose output nobody knows
    how to check produces artifacts nobody can trust.

    **Scoped to agents that can actually publish**, which is the honest form of the rule. The rule
    used to say *every* agent, and the judge broke it - correctly. The judge produces a **verdict**,
    not a deliverable; demanding that it declare a suite to check its own verdict is asking who
    verifies the verifier, and the answer to that is the Ed25519 signature, not another
    suite.

    `frame.publish` is the only door, and there are exactly two ways through it:
    holding the tool, or `publishes_output=True` (the system publishes the run's
    output). Either one makes an agent a producer - checkable rather than remembered. An agent that
    makes every format skill's kind is checked by each kind's own suite (`ARTIFACT_TYPES`).
    """
    if not agent.publishes:
        return
    if agent.makes_formats:  # each kind it makes is checked by that kind's own suite
        assert all(ARTIFACT_TYPES[kind].verifier_suite for kind in agent.kinds), agent.kinds
        return
    assert agent.verifier_suite is not None, f"{agent.name} can publish but declares no verifier suite"


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_tool_grants_are_minimal_and_named(agent: AgentDef) -> None:
    """Minimal tool grants. Every tool is named - there is no wildcard, because only
    granted tools mount and "everything" names none."""
    assert all(t and "*" not in t for t in agent.tools)


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_the_connect_block_is_its_own_grant(agent: AgentDef) -> None:
    """`request_connector` is a declared grant, not a side effect of `can_ask_user`: a step on the browser
    may ask the person (a login it was not given) and is never pointed at a connector, since a step told to
    connect first never opens the page. A connect request is a deferral, so only an agent that can park on a
    gate may hold it; who holds it is the grants table's row, never a name test."""
    held = grants.held(str(agent.name), grants.DEPLOYED)
    if CONNECT_GRANT in agent.tools:
        assert agent.can_ask_user, f"{agent.name} holds {CONNECT_GRANT} but cannot park on a gate"
    assert (CONNECT_GRANT in agent.tools) == ("request_connector" in held)
    assert agent.can_ask_user == ("ask_user" in held)
    if agent.browses:
        assert CONNECT_GRANT not in browsing(agent, on=True).tools


def test_the_identity_speaks_as_plane() -> None:
    """The one shared identity: Plane the factory, Prismoid the worker, and the
    promise the product is named for. An identity that loses any of the three has stopped
    answering "who are you?"."""
    flowed = " ".join(IDENTITY.split())  # wrap-proof: the file hard-wraps at ~100 cols
    assert "Plane" in flowed
    assert "Prismoid" in flowed
    assert "verifies before it delivers" in flowed


def test_the_identity_keeps_the_machinery_out_of_what_the_person_reads() -> None:
    """Every seat recites what the identity says about itself: it names no seat or role (the "who are
    you?" answer listed them), and it tells each seat that paths and tool calls stay in tool arguments."""
    flowed = " ".join(IDENTITY.split()).lower()
    assert not any(word in flowed for word in ("seat", "maker", "operator", "researcher", "planner"))
    assert "paths and tool calls stay in tool arguments" in flowed


def test_the_identity_obeys_its_own_style_law() -> None:
    """The identity carries the em-dash ban, so an em dash inside it would be the law breaking
    itself in the one place every seat reads."""
    assert "—" not in IDENTITY


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_the_identity_is_carried_by_every_seat_but_the_graders(agent: AgentDef) -> None:
    """The judge declines the identity - its value is distance from the work,
    and a preamble that makes it one of the family softens exactly that. The page reader
    copies a page's words and never speaks in the house voice. Everyone else carries it, so
    "who am I?" has one answer everywhere it is asked."""
    assert agent.identity is (str(agent.name) not in {"judge", "page_reader"})


@pytest.mark.parametrize("agent", roster(), ids=lambda a: str(a.name))
def test_the_style_law_lives_once(agent: AgentDef) -> None:
    """The em-dash law's one home is the identity. An identity-carrying seat restating it in its
    role prompt is the paste-drift this ADR removed; a seat that declines the identity must
    keep its own copy or it loses the law entirely."""
    assert ("em dash" in agent.prompt) is (not agent.identity)


def test_instructions_prepend_the_identity_and_only_the_identity() -> None:
    """The seam: identity, then the role, separated and never woven - and a seat
    that declines runs on exactly its own prompt, no residue."""
    writer = get("writer")
    assert instructions_for(writer).startswith(IDENTITY)
    assert instructions_for(writer).endswith(writer.prompt)
    judge = get("judge")
    assert instructions_for(judge) == judge.prompt


def test_the_kill_switch_hides_no_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plannable roster is the ONE list the planner and the bot menu read. The operator acts through
    connectors with computer use off, so the switch hides no agent; it keeps planned steps off the browser."""
    from prismoid.conf import settings

    monkeypatch.setattr(settings.computer_use, "enabled", False)
    off = {str(a.name) for a in roster(plannable=True)}
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    assert {str(a.name) for a in roster(plannable=True)} == off
    assert "operator" in off


def test_the_lead_is_told_the_truth_about_who_publishes() -> None:
    """The roster said an agent that publishes nothing "Publishes: document" (`produces` defaults to it), so a
    plan placed it as a document's author; an outcome agent says its report is the deliverable."""
    from prismoid.operations.planning_brief import roster_entry

    operator, writer = get("operator"), get("writer")
    assert operator.delivers_outcome and writer.publishes
    assert "Publishes: an outcome" in roster_entry(operator)
    assert "Publishes: document" in roster_entry(writer)


def test_every_looping_step_agent_is_guarded() -> None:
    """Every top-level step agent with hands (an asking agent granted a frame or web tool) rides the
    run guard: its brakes, and a retry that takes up its run on the call log.
    This replaced "only the computer is guarded": with the guard on the computer alone, a provider
    blip or a sandbox hiccup threw away a writer's, a maker's or a researcher's whole conversation.
    A seat that cannot ask may opt in too (the scatter map), but only one with hands to loop on."""
    hands = {str(a.name) for a in roster() if any(t.startswith(("frame.", "web.")) for t in a.tools)}
    looping = {str(a.name) for a in roster() if a.can_ask_user} & hands
    guarded = {str(a.name) for a in roster() if a.guarded}
    assert {"operator", "researcher", "maker", "writer"} <= looping
    assert looping <= guarded <= hands
    assert definition_for("researcher", TaskDeps(lane_role="scatter_map")).guarded


def test_the_plan_vocabulary_names_the_operator_and_no_retired_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The roster section the Lead reads is built from `roster(plannable=True)` headings: the
    operator is a word in it, the computer, the navigator and the builder are not."""
    from prismoid.conf import settings
    from prismoid.operations.planning_brief import roster_entry

    monkeypatch.setattr(settings.computer_use, "enabled", True)
    headings = {
        line.removeprefix("## ")
        for a in roster(plannable=True)
        for line in roster_entry(a).splitlines()
        if line.startswith("## ")
    }
    assert "operator" in headings and not headings & {"computer", "navigator", "builder"}


def test_the_code_skill_states_the_code_artifact_contract() -> None:
    """The computer's copy of the code contract is gone: the `code` skill, which seats a program step, tells it
    to ship a `test.sh` and publish the directory once (the code suite fails a code artifact without one)."""
    from prismoid.ai.skills.deliverables import BUILTIN_DIR
    from prismoid.ai.skills.format import parse_dir

    body = parse_dir(BUILTIN_DIR / "code").body
    assert "test.sh" in body and "type `code`" in body and "directory" in body
