# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bot format door: the one place the write-time doctrine is enforced.

Every rule here has a reason, and the reason is always the same shape - a bot that breaks it
passes review and fails in production, quietly. The tests are written so that a failure names
the rule rather than a line number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prismoid.ai.bots import format as bot_format
from prismoid.ai.bots.format import BOT_FILE, BOTS_DIR, CASES_FILE, NAMESPACE, BotError
from prismoid.ai.plugins import format as plugin_format
from prismoid.schemas.bots import MAX_BOT_CASES, BotCase, BotManifest
from prismoid.schemas.ids import BotName, SkillName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType

DESCRIPTION = (
    "Reviews vendor contracts against our standard terms, clause by clause. "
    "Load when: an MSA, DPA or SOW needs a close read. Not for: drafting contracts or legal advice."
)
INSTRUCTIONS = "Read every clause. Compare it to the standard terms. Flag each deviation."
CASES = """
selection:
  - goal: Review this MSA against our standard terms
    expect: contract-reviewer
  - goal: Write me a haiku about the sea
    expect: none
"""


def bot_md(**front: object) -> str:
    fields: dict[str, object] = {
        "name": "contract-reviewer",
        "description": DESCRIPTION,
        "capability": "frontier-reasoner",
        "produces": "document",
        "tools": ["frame.read_file"],
    }
    fields.update(front)
    lines = []
    for key, value in fields.items():
        if value is None:
            continue
        lines.append(f"{key}: {value!r}" if isinstance(value, str) else f"{key}: {value}")
    return "---\n" + "\n".join(lines) + "\n---\n" + INSTRUCTIONS + "\n"


# ── it parses ─────────────────────────────────────────────────────────────────────────────────


def test_a_well_formed_bot_parses() -> None:
    parsed = bot_format.parse("contract-reviewer", bot_md(), CASES)
    assert parsed.manifest.capability is ModelClass.FRONTIER_REASONER
    assert parsed.manifest.produces is ArtifactType.DOCUMENT
    assert parsed.manifest.tools == ("frame.read_file",)
    assert parsed.instructions.startswith("Read every clause")
    assert [c.expect for c in parsed.cases] == ["contract-reviewer", "none"]


def test_defaults_are_the_safe_ones() -> None:
    """An author who says nothing gets a prose bot on the cheap tier that may ask questions."""
    parsed = bot_format.parse("contract-reviewer", bot_md(capability=None, produces=None, tools=None), CASES)
    assert parsed.manifest.capability is ModelClass.FAST_EXECUTOR
    assert parsed.manifest.produces is ArtifactType.DOCUMENT
    assert parsed.manifest.tools == ()
    assert parsed.manifest.can_ask_user


def test_skills_and_connectors_accept_a_list_or_a_string() -> None:
    """Frontmatter is hand-written. `skills: extract` and `skills: [extract]` mean the same
    thing to a human, so they mean the same thing here."""
    listed = bot_format.parse("contract-reviewer", bot_md(skills=["extract", "critique"]), CASES)
    inline = bot_format.parse("contract-reviewer", bot_md(skills="extract, critique"), CASES)
    assert listed.manifest.skills == inline.manifest.skills == ("extract", "critique")


# ── it refuses ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("skill", ["../../escape", "x;id", "Report", "a--b"])
def test_a_skill_pin_that_is_not_a_legal_skill_name_is_refused(skill: str) -> None:
    with pytest.raises(BotError, match="not a legal skill name") as refused:
        bot_format.parse("contract-reviewer", bot_md(skills=[skill]), CASES)
    assert "skills" in refused.value.fields


def test_frontmatter_is_required() -> None:
    with pytest.raises(BotError, match="frontmatter"):
        bot_format.parse("contract-reviewer", INSTRUCTIONS, CASES)


def test_the_declared_name_must_match_the_directory() -> None:
    """Otherwise `pull` writes one name and `push` stores another, and the drift is invisible."""
    with pytest.raises(BotError, match="must match the directory"):
        bot_format.parse("contract-reviewer", bot_md(name="something-else"), CASES)


@pytest.mark.parametrize(
    "name", ["Contract-Reviewer", "-reviewer", "reviewer-", "contract--reviewer", "a" * 65, "rev iewer"]
)
def test_malformed_names_are_refused(name: str) -> None:
    """The Agent Skills name rule, adopted so a bundle stays portable to any conformant client."""
    with pytest.raises(BotError, match="a bot name is"):
        bot_format.parse(name, bot_md(name=name), CASES)


def test_a_reserved_name_is_refused_at_the_door() -> None:
    with pytest.raises(BotError, match="reserved"):
        bot_format.parse("one", bot_md(name="one"), CASES)


def test_a_name_that_leaks_a_model_is_refused() -> None:
    with pytest.raises(BotError, match="names a model"):
        bot_format.parse("claude-reviewer", bot_md(name="claude-reviewer"), CASES)


@pytest.mark.parametrize(
    ("description", "rule"),
    [
        ("Reviews contracts. Not for: drafting.", "Load when"),
        ("Load when: a contract needs review.", "Not for"),
        ("Load when: x. Not for: y. " + "word " * 60, "over the"),
        ("Load when a. Not for", "say what the bot is for"),
    ],
)
def test_a_written_description_must_be_a_routing_surface(description: str, rule: str) -> None:
    """The grammar is enforced on whatever IS written; an empty one is a newborn (below)."""
    with pytest.raises(BotError, match=rule):
        bot_format.parse("contract-reviewer", bot_md(description=description), CASES)


def test_a_newborn_parses_with_nothing_but_a_name() -> None:
    """Only the name is mandatory at birth. Description, instructions,
    and cases are the PUBLISH gate's demands - the door writes an empty-but-valid draft."""
    bare = "---\nname: 'contract-reviewer'\n---\n\n"
    parsed = bot_format.parse("contract-reviewer", bare, "")
    assert parsed.manifest.description == ""
    assert parsed.instructions == ""
    assert parsed.cases == ()


def test_instructions_over_budget_are_refused() -> None:
    fat = "---\n" + f"name: 'contract-reviewer'\ndescription: {DESCRIPTION!r}\n" + "---\n" + ("x " * 20_000)
    with pytest.raises(BotError, match="over the"):
        bot_format.parse("contract-reviewer", fat, CASES)


@pytest.mark.parametrize("capability", ["judge", "image-gen"])
def test_lane_reserved_capabilities_are_refused(capability: str) -> None:
    with pytest.raises(BotError, match="lane-reserved"):
        bot_format.parse("contract-reviewer", bot_md(capability=capability), CASES)


def test_an_unknown_capability_lists_the_real_ones() -> None:
    with pytest.raises(BotError, match="fast-executor"):
        bot_format.parse("contract-reviewer", bot_md(capability="genius"), CASES)


def test_an_unknown_tool_grant_is_refused() -> None:
    """Grants are a name allowlist; a grant no toolset knows mounts nothing."""
    with pytest.raises(BotError, match="unknown tool grant"):
        bot_format.parse("contract-reviewer", bot_md(tools=["frame.read_file", "frame.rm_rf"]), CASES)


def test_a_non_document_bot_must_hold_the_publish_grant() -> None:
    with pytest.raises(BotError, match=r"missing frame\.publish|must hold"):
        bot_format.parse("contract-reviewer", bot_md(produces="spreadsheet"), CASES)


def test_a_bot_with_no_cases_is_writable_but_carries_none() -> None:
    """Cases moved to the gate's demands: the door writes the draft; `bots test`
    and publish name the gap with the fix."""
    parsed = bot_format.parse("contract-reviewer", bot_md(), "")
    assert parsed.cases == ()


def test_a_case_may_only_expect_this_bot_or_none() -> None:
    """A case expecting some third bot is testing that bot's description, not this one's."""
    cases = "selection:\n  - goal: Do a thing\n    expect: some-other-bot\n"
    with pytest.raises(BotError, match="may only expect"):
        bot_format.parse("contract-reviewer", bot_md(), cases)


def test_too_many_cases_are_refused() -> None:
    many = "selection:\n" + "".join(
        f"  - goal: Goal number {i}\n    expect: contract-reviewer\n" for i in range(MAX_BOT_CASES + 1)
    )
    with pytest.raises(BotError, match="over the"):
        bot_format.parse("contract-reviewer", bot_md(), many)


# ── it round-trips, through the plugin layout ─────────────────────────────────────────────────


def test_render_round_trips_through_parse() -> None:
    """`pull → edit → push` must not corrupt a bot that was not edited. The colon in "Not for: …"
    is the specific hazard: an unquoted YAML scalar containing `: ` reads back as a mapping."""
    manifest = BotManifest(
        name=BotName("contract-reviewer"),
        description=DESCRIPTION,
        capability=ModelClass.FRONTIER_REASONER,
        tools=("frame.read_file",),
        skills=(SkillName("extract"),),
    )
    rendered = bot_format.render_bot_md(manifest, INSTRUCTIONS)
    cases = bot_format.render_cases([BotCase(goal="Review this MSA", expect="contract-reviewer")])
    back = bot_format.parse("contract-reviewer", rendered, cases)
    assert back.manifest == manifest.model_copy(update={"instructions_sha256": manifest.instructions_sha256})
    assert back.instructions == INSTRUCTIONS


def test_a_plugin_directory_is_the_on_disk_format(tmp_path: Path) -> None:
    plugin = tmp_path / "contract-review"
    bot_dir = plugin / NAMESPACE / BOTS_DIR / "contract-reviewer"
    bot_dir.mkdir(parents=True)
    (plugin / "plugin.json").write_text(plugin_format.render_plugin_manifest("contract-review"))
    (bot_dir / BOT_FILE).write_text(bot_md())
    (bot_dir / CASES_FILE).write_text(CASES)

    assert bot_format.bots_in(plugin) == [bot_dir]
    # Pointed at the plugin root OR at the bot directory - an author uses whichever they have.
    assert bot_format.parse_dir(plugin).manifest.name == "contract-reviewer"
    assert bot_format.parse_dir(bot_dir).manifest.name == "contract-reviewer"


def test_the_plugin_manifest_is_conformant(tmp_path: Path) -> None:
    """v1 requires exactly `$schema` and `name`; the schema id must be the canonical one, because
    a client selects its supported version by matching that string."""
    import json

    manifest = json.loads(plugin_format.render_plugin_manifest("contract-review", "Reviews contracts"))
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert manifest["name"] == "contract-review"


def test_a_plugin_with_two_bots_refuses_to_guess(tmp_path: Path) -> None:
    plugin = tmp_path / "pair"
    for name in ("contract-reviewer", "invoice-reader"):
        d = plugin / NAMESPACE / BOTS_DIR / name
        d.mkdir(parents=True)
        (d / BOT_FILE).write_text(bot_md(name=name))
        (d / CASES_FILE).write_text(CASES.replace("contract-reviewer", name))
    assert len(bot_format.bots_in(plugin)) == 2
    with pytest.raises(BotError, match="name the one you mean"):
        bot_format.parse_dir(plugin)


def test_a_directory_that_is_not_a_bot_says_so(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(BotError, match=r"no BOT\.md here"):
        bot_format.parse_dir(tmp_path / "empty")


# ── the structured routing fields ─────────────────────────────────────────────────────────────


def test_structured_keys_compose_the_description() -> None:
    md = bot_md(
        description=None,
        job="Reviews vendor contracts against our standard terms",
        use_when=["an MSA, DPA or SOW needs a close read"],
        not_for=["drafting contracts", "legal advice"],
    )
    parsed = bot_format.parse("contract-reviewer", md, CASES)
    assert parsed.manifest.description == (
        "Reviews vendor contracts against our standard terms. "
        "Load when an MSA, DPA or SOW needs a close read. "
        "Not for drafting contracts; legal advice."
    )
    assert parsed.manifest.job.startswith("Reviews vendor")
    assert parsed.manifest.use_when == ("an MSA, DPA or SOW needs a close read",)
    assert parsed.manifest.not_for == ("drafting contracts", "legal advice")


def test_a_verbatim_description_outranks_the_structured_keys() -> None:
    md = bot_md(job="Something else entirely", use_when=["never"], not_for=["nothing"])
    parsed = bot_format.parse("contract-reviewer", md, CASES)
    assert parsed.manifest.description == DESCRIPTION  # the typed sentence won


@pytest.mark.parametrize(
    ("front", "missing"),
    [
        ({"job": "", "use_when": ["x"], "not_for": ["y"]}, "one sentence"),
        ({"job": "Does things", "use_when": [], "not_for": ["y"]}, "trigger"),
        ({"job": "Does things", "use_when": ["x"], "not_for": []}, "boundary"),
    ],
)
def test_a_missing_structured_field_names_itself(front: dict[str, object], missing: str) -> None:
    md = bot_md(description=None, **front)
    with pytest.raises(BotError, match=missing):
        bot_format.parse("contract-reviewer", md, CASES)


def test_structured_keys_round_trip_through_render() -> None:
    from prismoid.schemas.bots import compose_description

    manifest = BotManifest(
        name=BotName("contract-reviewer"),
        description=compose_description(
            "Reviews vendor contracts", ["an MSA needs a read"], ["legal advice"]
        ),
        job="Reviews vendor contracts",
        use_when=("an MSA needs a read",),
        not_for=("legal advice",),
        capability=ModelClass.FRONTIER_REASONER,
        tools=("frame.read_file",),
    )
    md = bot_format.render_bot_md(manifest, INSTRUCTIONS)
    parsed = bot_format.parse("contract-reviewer", md, CASES)
    assert parsed.manifest.job == "Reviews vendor contracts"
    assert parsed.manifest.use_when == ("an MSA needs a read",)
    assert parsed.manifest.not_for == ("legal advice",)
    assert parsed.manifest.description == manifest.description


def test_the_composed_description_still_passes_the_grammar_check() -> None:
    from prismoid.schemas.bots import compose_description

    composed = compose_description("Writes release notes", ["a release ships"], ["blog posts"])
    lowered = composed.lower()
    assert "load when" in lowered and "not for" in lowered


# ── the structured keys' caps are door rules (found in review) ───────────────────────────────


def test_a_scalar_phrase_is_one_phrase_not_one_per_word() -> None:
    md = bot_md(
        description=None,
        job="Reviews contracts",
        use_when="a contract needs review",
        not_for="legal advice",
    )
    parsed = bot_format.parse("contract-reviewer", md, CASES)
    assert parsed.manifest.use_when == ("a contract needs review",)
    assert parsed.manifest.not_for == ("legal advice",)
    assert "Load when a contract needs review." in parsed.manifest.description


def test_over_cap_routing_fields_beside_a_verbatim_description_are_a_named_refusal() -> None:
    md = bot_md(use_when=["a", "b", "c", "d", "e"])
    with pytest.raises(BotError) as caught:
        bot_format.parse("contract-reviewer", md, CASES)
    assert "use_when" in caught.value.fields
    md = bot_md(job="x" * 300)
    with pytest.raises(BotError) as caught:
        bot_format.parse("contract-reviewer", md, CASES)
    assert "job" in caught.value.fields


def test_cases_of_carries_the_golden_cases_round_trip() -> None:
    manifest = {
        "cases": [{"goal": "Review this MSA", "expect": "contract-reviewer"}],
        "golden": [{"goal": "Find the indemnity clause", "must_contain": ["indemnity"]}],
    }
    cases, golden = bot_format.cases_of(manifest, default_expect="contract-reviewer")
    rendered = bot_format.render_cases(cases, golden)
    parsed = bot_format.parse("contract-reviewer", bot_md(), rendered)
    assert [g.goal for g in parsed.golden] == ["Find the indemnity clause"]
    assert parsed.golden[0].must_contain == ("indemnity",)


def test_an_alias_bomb_in_the_frontmatter_is_refused_at_once() -> None:
    """The loader refuses the first YAML alias, so nine nested levels never expand."""
    bomb = "a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]\n" for i in range(1, 10)
    )
    with pytest.raises(BotError, match="not valid YAML"):
        bot_format.parse(
            "contract-reviewer", f"---\nname: contract-reviewer\n{bomb}---\n{INSTRUCTIONS}", CASES
        )
