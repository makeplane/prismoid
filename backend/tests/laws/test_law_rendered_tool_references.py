# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every tool a rendered text names is a tool its reader holds (`ai/agents/grants.py`), and the
grants table is what the mounts send today.

A prompt that tells an agent to call a tool it lacks spends a turn on a call that cannot exist, or
teaches it a hand it never gets. The population is derived from code, per agent and per deployment
profile (computer use, the device, a search engine, an image model): each roster agent's first
request through the real `build()` (its instructions and every tool description it is sent), the
step brief `prompt_for` renders for each plannable agent, the express brief for the express lane's
agent, every bundled skill as the brief inlines it for each plannable agent whose pin it honours
(its declared `allowed-tools`, `grants.skill_fits`), and every skill a frameless agent's menu lets it read (`grants.SKILLS`). A tool is named by
its catalog name, as a whole word when the name has an underscore, in backticks when it is also an
English word (`shell`, `publish`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import itertools
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import pytest

from prismoid.ai import config
from prismoid.ai.agents import grants
from prismoid.ai.agents.base import REGISTRY, AgentDef, browsing, frame_profile_for, get, roster
from prismoid.ai.context.brief import prompt_for
from prismoid.ai.context.inline import Included, included_section
from prismoid.ai.skills.format import parse_dir, render
from prismoid.ai.tools import catalog
from prismoid.ai.tools.ask import CONNECT_GRANT
from prismoid.conf import SearchSettings
from prismoid.operations.execution.express import ambient_facts, express_prompt
from prismoid.operations.plan_shapes import lead_plan
from prismoid.operations.skills import BUILTIN_DIR
from prismoid.schemas.contract import Contract, SkillPin
from prismoid.schemas.ids import AgentName, SkillName
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.thread import ThreadTranscript
from tests.support.first_request import Sent, first_request


@dataclass(frozen=True)
class Profile:
    """One deployment a run may meet."""

    computer: bool
    device: bool
    search: bool
    image: bool

    def met(self) -> frozenset[grants.Needs]:
        """The grant needs this deployment provides."""
        met: set[grants.Needs] = set()
        if self.search:
            met.add("search")
        if self.image:
            met.add("image_model")
        if self.device and self.computer:
            met.add("device_browser")
        if self.computer:
            met.add("browser")  # a browsing agent's step is read on the browser while computer use is on
        return frozenset(met)


PROFILES: Final = tuple(Profile(*flags) for flags in itertools.product((False, True), repeat=4))
FULL: Final = Profile(computer=True, device=True, search=True, image=True)


@dataclass(frozen=True)
class Reference:
    """A rendered text naming a tool its reader does not hold."""

    agent: str
    text: str
    tool: str


@contextmanager
def _deployment(profile: Profile) -> Generator[None]:
    """The settings `profile` describes, for the length of the block."""
    current = config.current()
    search = (
        SearchSettings(engines="searxng", urls="searxng=http://localhost:8888")
        if profile.search
        else SearchSettings(engines="")
    )
    computer_use = current.computer_use.model_copy(update={"enabled": profile.computer})
    with config.use_settings(current.model_copy(update={"search": search, "computer_use": computer_use})):
        yield


async def _first_request(name: str, profile: Profile) -> Sent:
    """The tools and texts `name`'s first request carries under `profile`."""
    browser = frame_profile_for(name, browser=profile.computer, device=profile.device) == "browser"
    return await first_request(name, browser=browser, image=profile.image, chat=True)


@functools.cache
def _skills() -> dict[str, str]:
    """Every bundled skill, as the brief inlines it (read once: the files do not change in a run)."""
    texts: dict[str, str] = {}
    for directory in sorted(p for p in BUILTIN_DIR.iterdir() if (p / "SKILL.md").exists()):
        parsed = parse_dir(directory)
        path = f"/skills/{directory.name}/SKILL.md"
        texts[f"skill {directory.name}"] = included_section(
            [Included(path, render(parsed.manifest, parsed.body), data=False)]
        )
    return texts


@functools.cache
def _needs() -> dict[str, tuple[str, ...]]:
    """Every bundled skill's declared `allowed-tools`: the pin check's input."""
    return {
        f"skill {p.name}": parse_dir(p).manifest.allowed_tools
        for p in sorted(BUILTIN_DIR.iterdir())
        if (p / "SKILL.md").exists()
    }


def _seat(name: str, profile: Profile) -> AgentDef:
    """The definition a step of `name` runs as under `profile`: a browsing agent on the browser while computer use is on."""
    definition = get(name)
    return browsing(definition, on=profile.computer) if definition.browses else definition


def _step_brief(name: str, profile: Profile) -> str:
    """The brief a plannable agent's step reads: a source to read, a pinned skill, a failed attempt."""
    definition = _seat(name, profile)
    spec = StepSpec(
        agent=AgentName(name),
        contract=Contract(goal="Write the memo.", acceptance=["it is right"], inputs=["/task/inputs/a.csv"]),
        skill_pins=[SkillPin(name=SkillName("report"), version=1)],
    )
    return prompt_for(
        spec,
        ["the figure was ungrounded"],
        asker=CONNECT_GRANT in definition.tools,
        guarded=definition.guarded,
    )


def _express_brief(profile: Profile) -> tuple[str, str]:
    """The express step's agent (the Lead) and the brief it reads."""
    (_, spec), *_ = lead_plan("what is the weather in Paris?")[0]
    now = dt.datetime(2026, 11, 14, 9, tzinfo=dt.UTC)
    brief = express_prompt(
        prompt_for(spec, []),
        ThreadTranscript(),
        ambient=ambient_facts("UTC", now, services=[], computer=profile.computer),
    )
    return str(spec.agent), brief


_SPELLINGS: Final = {
    tool.name: re.compile(rf"(?<![\w.]){tool.name}(?!\w)" if "_" in tool.name else rf"`{tool.name}[`(]")
    for tool in catalog.TOOLS
}


def named(text: str) -> frozenset[str]:
    """The catalog tools `text` names."""
    return frozenset(name for name, spelling in _SPELLINGS.items() if spelling.search(text))


def _unheld(agent: str, texts: dict[str, str], met: frozenset[grants.Needs]) -> set[Reference]:
    held = grants.held(agent) | grants.held(agent, met)  # anywhere it runs, and in this profile's seat
    return {Reference(agent, text, tool) for text, body in texts.items() for tool in named(body) - held}


async def _references(profile: Profile, agents: list[str] | None = None) -> set[Reference]:
    """Every rendered reference under `profile` to a tool its reader does not hold."""
    found: set[Reference] = set()
    with _deployment(profile):
        population = agents if agents is not None else [str(a.name) for a in roster()]
        skills = _skills()
        for name in population:
            met = profile.met()
            found |= _unheld(name, (await _first_request(name, profile)).texts, met)
            found |= _unheld(
                name, {f"skill {s}": skills[f"skill {s}"] for s in grants.SKILLS.get(name, ())}, met
            )
        for definition in roster(plannable=True):
            name = str(definition.name)
            if agents is None or name in agents:
                tools = _seat(name, profile).tools
                honoured = {k: text for k, text in skills.items() if grants.skill_fits(tools, _needs()[k])}
                found |= _unheld(name, {"step brief": _step_brief(name, profile), **honoured}, profile.met())
        agent, brief = _express_brief(profile)
        if agents is None or agent in agents:
            found |= _unheld(agent, {"express brief": brief}, profile.met())
    return found


# What the law found the day it landed: real defects, listed so they can only shrink. The fix is the
# text or the grant, and the row goes with it.
KNOWN: Final[frozenset[Reference]] = frozenset()


async def test_every_rendered_tool_reference_is_held() -> None:
    found: set[Reference] = set()
    for profile in PROFILES:
        found |= await _references(profile)
    new = sorted(found - KNOWN, key=str)
    assert not new, "texts name tools their reader does not hold:\n" + "\n".join(map(str, new))
    stale = sorted(KNOWN - found, key=str)
    assert not stale, "fixed; drop from KNOWN:\n" + "\n".join(map(str, stale))


async def test_the_law_bites_on_a_planted_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    judge = get("judge")
    planted = dataclasses.replace(judge, prompt=judge.prompt + "\nCheck the claim with `web_search` first.")
    monkeypatch.setitem(REGISTRY, AgentName("judge"), planted)
    found = await _references(FULL, agents=["judge"])
    assert Reference("judge", "instructions", "web_search") in found


def test_a_tool_is_named_by_its_spelling_not_by_the_word() -> None:
    assert named("then publish it, and read_file the inputs") == {"read_file"}
    assert named("run it with the `shell` tool, then `publish(path=...)`") == {"shell", "publish"}
    assert named("the grant frame.read_file, the field web_fetches") == frozenset()


async def _drift(profiles: tuple[Profile, ...], agents: list[str] | None = None) -> list[str]:
    """Where the tools a first request is sent differ from what the grants table holds."""
    drift: list[str] = []
    for profile in profiles:
        with _deployment(profile):
            for name in agents if agents is not None else [str(a.name) for a in roster()]:
                sent = (await _first_request(name, profile)).tools
                if sent != (want := grants.held(name, profile.met())):
                    drift.append(
                        f"{name} {profile}: sent only {sorted(sent - want)}, table only {sorted(want - sent)}"
                    )
    return drift


async def test_the_mounts_send_exactly_what_the_table_grants() -> None:
    drift = await _drift(PROFILES)
    assert not drift, "ai/agents/grants.py and the mounts disagree:\n" + "\n".join(drift)
    named = {str(a.name) for a in roster()}
    assert set(grants.GRANTS) <= named, "a grants row names no agent"


async def test_the_table_check_bites_on_a_grant_the_mounts_do_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(grants.GRANTS, "judge", (*grants.GRANTS["judge"], grants.Grant("shell")))
    assert await _drift((FULL,), agents=["judge"]) == [f"judge {FULL}: sent only [], table only ['shell']"]
