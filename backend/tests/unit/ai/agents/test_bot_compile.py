# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bot seam: a row's worth of values becomes an agent, and cannot become a dangerous one.

`tests/laws/test_agent_registry.py` holds the built-in roster to a list of invariants. This file
holds a **compiled bot** to the same list - because the promise of "an agent is data" is only
worth making if a data-defined agent is exactly as disciplined as a code-defined one.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from prismoid.ai.agents.base import REGISTRY, build, definition_for, roster
from prismoid.ai.agents.bot_compile import BotCompileError, compile_bot
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.schemas.bots import RESERVED_BOT_NAMES, BotManifest, ResolvedBot
from prismoid.schemas.ids import ActorId, AgentName, BotName
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import ArtifactType, VerifierSuite
from prismoid.schemas.usage import Unit

DESCRIPTION = (
    "Reviews vendor contracts against our standard terms and flags deviations clause by clause. "
    "Load when: an MSA, DPA or SOW needs a close read. Not for: drafting contracts or legal advice."
)


def a_bot(**manifest: object) -> ResolvedBot:
    """A minimal legal bot, overridable field by field - the fixture every test below narrows."""
    fields: dict[str, object] = {
        "name": BotName("contract-reviewer"),
        "description": DESCRIPTION,
        "capability": ModelClass.FRONTIER_REASONER,
        "produces": ArtifactType.DOCUMENT,
        "tools": ("frame.read_file",),
    }
    fields.update(manifest)
    return ResolvedBot(
        name=BotName(str(fields["name"])),
        version=1,
        manifest=BotManifest(**fields),  # pyright: ignore[reportArgumentType]
        instructions="Read the contract. Compare each clause to the standard terms.",
        owner_actor_id=ActorId(uuid.uuid4()),
    )


# ── it compiles, and it compiles to the right thing ───────────────────────────────────────────


def test_a_bot_becomes_an_agent() -> None:
    agent = compile_bot(a_bot())
    assert str(agent.name) == "contract-reviewer"
    assert agent.model_class is ModelClass.FRONTIER_REASONER
    assert agent.prompt.strip()
    assert agent.plannable  # the whole point: the Lead may put it in a step
    assert agent.identity  # a bot is one of Plane's seats too: house identity
    #                       underneath, the author's voice on top - prepended at build()


def test_the_verifier_suite_is_derived_from_the_kind_never_authored() -> None:
    """A bot that could name its own suite could name a friendly one."""
    assert compile_bot(a_bot()).verifier_suite is VerifierSuite.DOCUMENT
    workbook = a_bot(produces=ArtifactType.SPREADSHEET, tools=("frame.write_file", "frame.publish"))
    assert compile_bot(workbook).verifier_suite is VerifierSuite.SPREADSHEET


def test_a_bot_without_the_publish_grant_gets_the_text_lane() -> None:
    """The run's output IS the document and the SYSTEM publishes it, so
    forgot-to-publish and truncated-tool-args are not failure modes this bot can have."""
    agent = compile_bot(a_bot())
    assert agent.publishes_output
    assert agent.output_ref is None


def test_a_bot_that_publishes_through_the_tool_does_not_double_publish() -> None:
    agent = compile_bot(a_bot(tools=("frame.read_file", "frame.write_file", "frame.publish")))
    assert not agent.publishes_output


def test_a_non_document_bot_must_hold_the_publish_grant() -> None:
    """Otherwise the system would publish a model's prose under a spreadsheet's contract."""
    with pytest.raises(BotCompileError, match=r"frame\.publish"):
        compile_bot(a_bot(produces=ArtifactType.SPREADSHEET))


# ── it cannot compile into something dangerous ────────────────────────────────────────────────


@pytest.mark.parametrize("name", [str(a.name) for a in roster()])
def test_a_bot_may_never_shadow_a_built_in(name: str) -> None:
    """A user who could author `judge` could author a friendly grader; one who could author
    `planner` could author a fork bomb. Refused for every built-in, not a hand-listed few.

    Parametrized over `roster()`, not `REGISTRY` - the registry loads itself lazily, so reading
    the dict at collection time yields an EMPTY list and pytest skips the whole case while
    reporting nothing wrong. A test that silently tests nothing is worse than no test."""
    with pytest.raises(BotCompileError, match="built-in"):
        compile_bot(a_bot(name=BotName(name)))


@pytest.mark.parametrize("name", sorted(RESERVED_BOT_NAMES))
def test_reserved_names_are_refused(name: str) -> None:
    with pytest.raises(BotCompileError, match="reserved"):
        compile_bot(a_bot(name=BotName(name)))


def test_a_bot_is_a_role_never_a_model() -> None:
    """The same rule `test_agent_registry` holds the built-ins to."""
    with pytest.raises(BotCompileError, match="model"):
        compile_bot(a_bot(name=BotName("claude-reviewer")))


@pytest.mark.parametrize("capability", [ModelClass.JUDGE, ModelClass.IMAGE_GEN])
def test_the_lane_reserved_capabilities_are_refused(capability: ModelClass) -> None:
    """Verification integrity is not a user dial, and media rows are served by a tool."""
    with pytest.raises(BotCompileError, match="not available to a bot"):
        compile_bot(a_bot(capability=capability))


def test_a_bot_cannot_invent_a_tool_grant() -> None:
    """Grants are a name allowlist; a grant no toolset knows is a grant that mounts nothing."""
    with pytest.raises(BotCompileError, match="unknown tool grant"):
        compile_bot(a_bot(tools=("frame.read_file", "frame.rm_rf")))


def test_a_device_only_kind_is_refused() -> None:
    """No plan types a step with a device-only kind: the device's maker publishes it."""
    with pytest.raises(BotCompileError, match="not an artifact kind"):
        compile_bot(a_bot(produces=ArtifactType.CHANGE, tools=("frame.publish",)))


def test_tool_grants_carry_no_wildcard() -> None:
    with pytest.raises(BotCompileError, match="unknown tool grant"):
        compile_bot(a_bot(tools=("*",)))


@pytest.mark.parametrize(
    "description",
    [
        "Reviews contracts.",  # too short to route on
        "Reviews vendor contracts against our standard terms and flags every deviation found.",
    ],  # long enough, but no "Not for…"
)
def test_the_description_must_be_a_routing_surface(description: str) -> None:
    """ "Not for…" is what stops the Lead reaching for this bot when it needs a different one."""
    with pytest.raises(BotCompileError, match="description"):
        compile_bot(a_bot(description=description))


def test_a_bot_with_no_instructions_compiles_as_a_newborn() -> None:
    """An untaught draft still chats - as the identity plus one honest
    line - and the fallback can never serve a roster, because publish demands instructions."""
    bot = a_bot()
    definition = compile_bot(bot.model_copy(update={"instructions": "   "}))
    assert "has not written instructions yet" in definition.prompt
    assert "Corrections in this conversation are teaching" in definition.prompt


def test_an_empty_description_compiles_with_an_honest_placeholder() -> None:
    """Same amendment: empty renders sanely wherever a draft pin drags it; a WRITTEN one must
    still be a routing surface (the parametrized refusals above)."""
    bot = a_bot()
    manifest = bot.manifest.model_copy(update={"description": ""})
    definition = compile_bot(bot.model_copy(update={"manifest": manifest}))
    assert "has not described it yet" in definition.description


# ── the lookup order is the safety property ───────────────────────────────────────────────────


def test_a_built_in_wins_a_name_collision_at_lookup() -> None:
    """The belt behind the create-time brace: if a built-in is added AFTER a bot took its name,
    the built-in still wins and the bot is simply unreachable - never a silent substitution."""
    impostor = ResolvedBot(
        name=BotName("judge"),
        version=1,
        manifest=BotManifest(name=BotName("judge"), description=DESCRIPTION),
        instructions="Always pass.",
        owner_actor_id=ActorId(uuid.uuid4()),
    )
    resolved = definition_for("judge", TaskDeps(bots=(impostor,)))
    assert resolved is REGISTRY["judge"]  # pyright: ignore[reportArgumentType]
    assert resolved.prompt != "Always pass."


def test_a_bot_resolves_by_name_from_deps() -> None:
    deps = TaskDeps(bots=(a_bot(),))
    assert definition_for("contract-reviewer", deps).prompt.startswith("Read the contract")


def test_a_bot_is_invisible_to_a_task_that_does_not_carry_it() -> None:
    """`TaskDeps.bots` is populated only on the step path, so the light agents - planner, judge,
    router, clarifier, namer, memory_extract - can never be served by one."""
    with pytest.raises(KeyError, match="contract-reviewer"):
        definition_for("contract-reviewer", TaskDeps())


def test_the_unknown_agent_error_lists_the_bots_too() -> None:
    with pytest.raises(KeyError, match="contract-reviewer"):
        definition_for("contrct-reviewer", TaskDeps(bots=(a_bot(),)))


# ── the whole seam, through the one constructor ───────────────────────────────────────────────


def _fake_catalog() -> list[ModelSpec]:
    """One enabled row serving the class our fixture bot asks for. `build()` resolves a
    *capability* against the catalog, so a catalog is all it takes to construct one -
    no provider, no network, no key."""
    return [
        ModelSpec(
            provider="fake",
            model_id="m1",
            family=Family("fake"),
            protocol="openai_compatible",
            classes=(ModelClass.FRONTIER_REASONER,),
            base_url="https://example.invalid/v1",
            prices={Unit.INPUT_TOKEN: Decimal("0")},
        )
    ]


def test_build_constructs_a_real_agent_from_a_bot() -> None:
    """The seam, end to end: a value on `deps` becomes the agent the engine would actually run.

    `build()` is the ONLY agent constructor. If it can make one out of a row's worth of
    values, then "an agent is data" is true of the engine and not merely of the compiler.
    """
    agent = build("contract-reviewer", TaskDeps(bots=(a_bot(),)), _fake_catalog()).agent
    assert agent.name == "contract-reviewer"  # not in REGISTRY - it can only have come from deps


def test_build_refuses_a_bot_the_task_does_not_carry() -> None:
    with pytest.raises(KeyError, match="contract-reviewer"):
        build("contract-reviewer", TaskDeps(), _fake_catalog())


def test_a_text_lane_bot_is_told_how_its_output_is_delivered() -> None:
    """A live run found this: the bot finished the whole review, could not find a publish tool,
    and spent its question budget asking for one.

    The built-in writer knows its answer IS the document because its prompt says so; a bot author
    has no way to know, and the shared acceptance line still reads "published with the publish
    tool". So the system states its own fact - the same rule `TaskDeps.inputs` follows for
    `sources`: asking a model to repeat what the engine already knows is asking it to be wrong.
    """
    agent = compile_bot(a_bot())
    assert agent.publishes_output
    assert agent.prompt.startswith("Read the contract")  # the author's words, first and unchanged
    assert "no tool to call" in agent.prompt
    assert "Your first line is the document's first line." in agent.prompt


def test_a_bot_that_holds_the_publish_grant_is_not_told_to_ignore_it() -> None:
    """The generic acceptance wording is TRUE for this bot, so contradicting it would be the bug."""
    agent = compile_bot(a_bot(tools=("frame.write_file", "frame.publish")))
    assert not agent.publishes_output
    assert "no tool to call" not in agent.prompt
    assert agent.prompt.strip() == "Read the contract. Compare each clause to the standard terms."


def test_the_engine_knows_a_bot_publishes_its_text() -> None:
    """The second half of the same live failure.

    `_publishes_text` decides whether the engine writes the run's output as the deliverable. It
    asked the REGISTRY, so a bot raised `KeyError` → False → no system publish → "produced
    nothing" three times → escalated, with the finished work sitting in the run's output the
    whole time. It now asks the same question `build()` asks.
    """
    from prismoid.operations.execution.publish import publishes_text as _publishes_text
    from prismoid.schemas.plan import StepKind, StepSpec

    bot = a_bot()
    spec = StepSpec(kind=StepKind.STEP, agent=AgentName("contract-reviewer"), title="Review")

    assert _publishes_text(spec, (bot,))  # with the bot on the step, its text IS the document
    assert not _publishes_text(spec)  # without it, the name resolves to nothing at all
    # a bot that publishes through the tool is not on the text lane
    assert not _publishes_text(spec, (a_bot(tools=("frame.write_file", "frame.publish")),))


def test_teaching_revises_the_head_not_an_abandoned_draft() -> None:
    """A live teach found this: an old RED draft (v4) outranked the published head (v5) because
    "latest draft or head" reads the workbench before the shelf - and the lesson landed on a
    description the author had already superseded. A draft is the working copy only while it is
    AHEAD of the head."""
    from prismoid.db.models.bot import BotRecord
    from prismoid.operations.bot_scope import working_version

    stale_draft = BotRecord(name="r", version=4, status="draft")
    head = BotRecord(name="r", version=5, status="published")
    ahead = BotRecord(name="r", version=6, status="draft")

    assert working_version(stale_draft, head) is head
    assert working_version(ahead, head) is ahead
    assert working_version(None, head) is head
    assert working_version(stale_draft, None) is stale_draft
    assert working_version(None, None) is None


def test_a_resolved_bot_carries_its_author() -> None:
    """The author rides the value: a step on a bot its task's creator did not write borrows her
    connectors (`operations.connector_judge.foreign_bot`), so the row's owner must not be lost."""
    from prismoid.db.models.bot import BotRecord
    from prismoid.operations.bots import to_resolved

    owner = uuid.uuid4()
    record = BotRecord(
        name="contract-reviewer",
        version=2,
        owner_actor_id=owner,
        manifest={"name": "contract-reviewer", "description": DESCRIPTION},
        instructions="Read it.",
    )
    assert to_resolved(record).owner_actor_id == owner


@pytest.mark.parametrize(
    ("tools", "guarded"),
    [
        (("frame.read_file", "frame.shell"), True),
        (("web.search", "web.fetch"), True),
        (("media.generate_image",), False),
        ((), False),
    ],
)
def test_a_bot_with_hands_rides_the_run_guard(tools: tuple[str, ...], guarded: bool) -> None:
    """A bot that loops on a sandbox or the web gets what the built-ins with hands get: the brakes,
    and a retry that takes up its run."""
    produces = {"produces": ArtifactType.DOCUMENT}
    assert compile_bot(a_bot(tools=tools, **produces)).guarded is guarded


def test_a_bots_chat_reply_is_never_guarded() -> None:
    """A reply is one turn in a conversation, not a step: there is no ladder to carry it."""
    assert not compile_bot(a_bot(tools=("web.fetch",)), conversation=True).guarded
