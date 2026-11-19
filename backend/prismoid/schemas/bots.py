# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bot shapes - a user-authored agent, as data.

A built-in agent is code: one `AgentDef` file plus one import in `ai/agents/all.py`. A **Bot**
is the same thing spelled as a row - *"everything a user composes becomes data; everything that
must be safe stays code."* The manifest here is the whole authored surface; the instructions are
the judgment, stored inline on the row (not in the object store: a bot is at most
five thousand tokens read once, so the object store would buy only dedup).

Two types, on purpose, mirroring `skills.py`: `ParsedBot` is what the format door produced from
an author's directory, and `ResolvedBot` is the version-pinned value that crosses into the
engine on `TaskDeps`. Neither is an `AgentDef` - the compile happens in `ai/agents/bot_compile.py`,
because `deps` sits *below* `agents` in the engine contract and may not hold one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.ids import ActorId, BotName, SkillName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType, MemoryProposal

# ── doctrine limits, enforced at the format door (W2) and named so a 422 can cite one ─────────

# The routing surface. Same budget as a skill's, and for the same reason: the planner reads this
# and nothing else to decide whether a bot applies, so it must fit a roster of them in a prompt.
MAX_DESCRIPTION_WORDS: Final = 50
MAX_DESCRIPTION_CHARS: Final = 600

# The structured half of the routing surface. The door's "Load when" / "Not for"
# grammar is correct semantics enforced on the wrong party: authors kept bouncing off typing it
# by hand. These caps bound the three fields the composer assembles the description FROM; the
# composed result still walks the description rules unchanged, so the eval scores the same
# surface it always did.
MAX_JOB_CHARS: Final = 240
MAX_ROUTING_PHRASES: Final = 4
MAX_PHRASE_CHARS: Final = 100

# The judgment. A bot whose instructions do not fit here is two bots, or wants a skill.
MAX_INSTRUCTIONS_TOKENS: Final = 5_000

# Selection cases: enough to prove the description is sharp, few enough to stay sub-minute.
MAX_BOT_CASES: Final = 20

# How many bots One is shown for one task. Past this the roster stops being a menu and starts
# being a wall of text the planner skims - the shortlist router is the named scale valve.
MAX_ROSTER_BOTS: Final = 12

# The tool vocabulary a bot may request. The frame is the containment boundary, so `shell` is
# fine (four built-ins hold it); what a bot may never do is invent a grant no toolset
# mounts. Every name here must exist in `ai/tools/frame_tools.py`.
BOT_TOOLS: Final = frozenset(
    {
        "frame.read_file",
        "frame.write_file",
        "frame.shell",
        "frame.publish",
        "media.generate_image",
        "web.fetch",  # the web door - read-only public web, same grant the built-ins hold
        "web.search",  # the search lane - absent unless the operator configured engines
        "routine.create",  # propose standing work - born PAUSED; the owner arms it
    }
)

# The model classes a bot may ask for. `JUDGE` is excluded because verification integrity is
# not a user dial (a bot that picks its own grader picks a friendly one), and `IMAGE_GEN` because
# media rows are served by a TOOL and never as a chat seat.
BOT_MODEL_CLASSES: Final = frozenset(
    {
        ModelClass.FAST_EXECUTOR,
        ModelClass.FRONTIER_EXECUTOR,
        ModelClass.FRONTIER_REASONER,
        ModelClass.LONG_CONTEXT,
        ModelClass.CHEAP_CLASSIFIER,
        ModelClass.CODE,
        ModelClass.VISION,
    }
)

# What a selection case means by "no bot should take this goal".
NO_BOT: Final = "none"

# Names a bot may never take, beyond whatever is in the live `REGISTRY`. Two kinds: words the
# product itself speaks ("one" is the front door and is code-owned, never a bot), and words a
# future built-in will plausibly want. Reserving them now costs an author nothing; reclaiming
# one later would silently make a published bot unreachable.
#
# It lives here rather than beside the compiler because the FORMAT DOOR must enforce it too -
# an author should meet this rule as a 422 naming the field, not as a failure at dispatch - and
# `ai/bots/` sits below `ai/agents/` in the engine contract.
RESERVED_BOT_NAMES: Final = frozenset(
    {
        "one",  # the front door - code-owned, because it proposes into the validated DAG
        "agent",
        "assistant",
        "bot",
        "prismoid",
        NO_BOT,  # the selection eval's "no bot should take this" sentinel
        "plane",
        "system",
        "user",
    }
)

# Model FAMILIES a bot name may not contain. An agent is a role - `contract-reviewer`, never a
# name with a model family in it - because what model serves the role is a routing decision made
# at run time. (Backticks, not quotes: `tools/audit.sh` greps for a quoted family name.)
#
# Families only, deliberately. `tests/laws/test_agent_registry.py` also refuses the bare version
# tokens "-4" and "-5" in a BUILT-IN's name, which is fine for eighteen hand-chosen names - but
# applied to user-authored ones it rejects `q4-planner`, `v5-reviewer` and `top-5-accounts`,
# which name no model at all. (Found by a test whose random suffix happened to contain "-5".)
# A family name is unambiguous; a digit after a hyphen is not.
MODEL_NAME_TOKENS: Final = (
    "gpt",
    "claude",
    "sonnet",
    "opus",
    "haiku",
    "gemini",
    "grok",
    "llama",
    "deepseek",
    "mistral",
    "qwen",
)


def compose_description(job: str, use_when: Sequence[str], not_for: Sequence[str]) -> str:
    """The three structured fields as the canonical routing sentence, one composer for the form and BOT.md.
    Raises `ValueError` naming the one missing field."""
    job = " ".join(job.split()).strip()
    triggers = [" ".join(p.split()).strip().rstrip(".") for p in use_when if p.strip()]
    boundaries = [" ".join(p.split()).strip().rstrip(".") for p in not_for if p.strip()]
    if not job:
        raise ValueError("say what the bot does, in one sentence")
    if not triggers:
        raise ValueError("add at least one trigger: when should this bot step in?")
    if not boundaries:
        raise ValueError("add at least one boundary: what should this bot refuse?")
    if len(triggers) > MAX_ROUTING_PHRASES or len(boundaries) > MAX_ROUTING_PHRASES:
        raise ValueError(f"at most {MAX_ROUTING_PHRASES} phrases per list - keep the surface tight")
    for phrase in (*triggers, *boundaries):
        if len(phrase) > MAX_PHRASE_CHARS:
            raise ValueError(f"keep each phrase under {MAX_PHRASE_CHARS} characters")
    if not job.endswith((".", "!", "?")):
        job += "."
    return f"{job} Load when {'; '.join(triggers)}. Not for {'; '.join(boundaries)}."


class BotManifest(BaseModel):
    """What the registry row stores about a bot version, all but the instructions; `description` is
    author text that reaches a planning prompt, so it is capped and fenced at render time."""

    model_config = ConfigDict(frozen=True)

    name: BotName
    # Empty is legal ON A DRAFT (nothing but the name is mandatory at
    # birth); publish demands the routing surface, so no description-less bot ever reaches a
    # roster. Completeness is the GATE's requirement, never the door's.
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)

    # The structured routing fields the description was composed from, when it was.
    # Kept beside the composition so the web edits meaning, never grammar, and a pull round-trips.
    # Empty on rows authored as verbatim prose - the description is always the canonical surface.
    job: str = Field(default="", max_length=MAX_JOB_CHARS)
    use_when: tuple[str, ...] = Field(default=(), max_length=MAX_ROUTING_PHRASES)
    not_for: tuple[str, ...] = Field(default=(), max_length=MAX_ROUTING_PHRASES)

    # A role's requirement, never a model string. What serves it is a routing decision
    # made from the catalog at run time.
    capability: ModelClass = ModelClass.FAST_EXECUTOR

    # What this bot's published deliverable IS. The verifier suite is *derived* from this and is
    # deliberately not authorable - see `ai/agents/bot_compile.compile_bot`.
    produces: ArtifactType = ArtifactType.DOCUMENT

    tools: tuple[str, ...] = ()  # granted by name; an ungranted tool is not mounted
    skills: tuple[SkillName, ...] = ()  # default pins; the plan may add more
    connectors: tuple[str, ...] = ()  # connector slugs, resolved against the RUNNER's own rows
    # The author's reason per declared slug - the access tab's "why", shown to a
    # viewer deciding whether to connect, and to a stranger reading a template preview. A
    # missing key renders honestly as no reason given; slugs stay the canonical list above.
    connector_why: dict[str, str] = Field(default_factory=dict)

    can_ask_user: bool = True  # a bot is a top-level step agent, so this is allowed

    instructions_sha256: str = ""  # content hash of the inline body; makes an unchanged push a no-op


class BotCase(BaseModel):
    """One selection case: a goal, and the bot that should take it (or `none`).

    This is the bot analogue of a skill's `RoutingCase`, and it is the publish gate. A bot whose
    description cannot win the goals it claims - or which grabs goals it should not - is a bot
    that will misroute silently in production, and publishing it is refused with a 409.
    """

    model_config = ConfigDict(frozen=True)

    goal: str = Field(min_length=1)
    expect: str = Field(min_length=1)  # this bot's name, or NO_BOT


class GoldenBotCase(BaseModel):
    """One end-to-end golden case a bot ships with (the advisory nightly lane): a real task
    the bot must win, `must_contain` checked against the delivered bytes.
    Never the publish gate - ADR-015 records why real-money runs cannot be."""

    model_config = ConfigDict(frozen=True)

    goal: str = Field(min_length=1)
    must_contain: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()


MAX_GOLDEN_CASES: Final = 5


class ParsedBot(BaseModel):
    """What the format door produced from an author's plugin directory - validated, not yet stored."""

    model_config = ConfigDict(frozen=True)

    manifest: BotManifest
    instructions: str = ""  # empty on a newborn draft; publish demands judgment
    cases: tuple[BotCase, ...] = ()
    golden: tuple[GoldenBotCase, ...] = ()


class BotSource(BaseModel):
    """How a bot arrived: either the raw files, or the fields.

    Two shapes for one resource, deliberately. The CLI holds a `BOT.md` and sends it verbatim.
    The web holds a form - a capability dropdown, tool checkboxes - and asking it to hand-assemble
    YAML would put a second renderer in TypeScript and a second set of quoting bugs with it.
    `operations/bots.py` renders the fields into the same bytes and walks both through the one
    format door, so there is one validation path and one set of error messages.

    `bot_md` wins when both are present: a file the author typed outranks a form assembled from it.
    """

    model_config = ConfigDict(frozen=True)

    bot_md: str | None = None
    cases_yaml: str = ""

    description: str | None = None
    # The structured half of the routing surface: when `description` is absent these
    # three compose it through `compose_description`, so a form author types meaning, not grammar.
    job: str | None = None
    use_when: tuple[str, ...] = ()
    not_for: tuple[str, ...] = ()
    instructions: str | None = None
    capability: str | None = None
    produces: str | None = None
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    connector_why: dict[str, str] = Field(default_factory=dict)
    can_ask_user: bool = True
    cases: tuple[BotCase, ...] = ()
    golden: tuple[GoldenBotCase, ...] = ()  # the advisory nightly cases, carried on edits


class ResolvedBot(BaseModel):
    """A bot pin turned into the values the engine is handed: the concrete version, its manifest,
    and its instructions. The row named the version; the loader fetched the body by hash.

    **This is what rides on `TaskDeps.bots`** - a value, never a row, and never an
    `AgentDef` (that lives one layer up). `operations` builds it; `ai/agents/bot_compile.compile_bot`
    turns it into the agent the engine actually runs.
    """

    model_config = ConfigDict(frozen=True)

    name: BotName
    version: int
    manifest: BotManifest
    instructions: str
    # The author. A step served by a bot its task's creator did not write runs her connectors
    # borrowed (`operations.connector_judge.foreign_bot`): the instructions are another member's words.
    owner_actor_id: ActorId

    def ref(self) -> str:
        return f"{self.name}@{self.version}"


# ── teaching (W4): distil a real run into a draft ─────────────────────────────────────────────


class DigestStep(BaseModel):
    """One plan step, as evidence: who ran it, how it ended, why it was chosen."""

    model_config = ConfigDict(frozen=True)

    agent: str
    title: str
    state: str
    why: str = ""


class DigestArtifact(BaseModel):
    """One deliverable and its receipt - the verdict is the sharpest teacher in the digest."""

    model_config = ConfigDict(frozen=True)

    filename: str
    type: str
    verdict: str = ""  # pass | fail | "" (unverified / pending)
    findings: tuple[str, ...] = ()  # failed check names + clipped detail


class RunDigest(BaseModel):
    """One finished run as the evidence a `from_run` lesson reads: a value built by
    `operations/bots.digest_run`, its transcript already rendered (rows never reach the model)."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    goal: str
    status: str
    steps: tuple[DigestStep, ...] = ()
    transcript_md: str = ""  # the whole conversation - the corrections live here
    tool_counts: dict[str, int] = Field(default_factory=dict)
    denials: tuple[str, ...] = ()  # what a tool or the proxy refused, deduped
    artifacts: tuple[DigestArtifact, ...] = ()


class BotLesson(BaseModel):
    """What `bot_lesson` proposes: the bot's next draft, whole.

    `instructions` is the COMPLETE revised text, never a patch - models write documents far more
    reliably than diffs, and `bots diff` renders the change for review anyway. A lesson is only
    ever a DRAFT: the author reads the diff, runs the eval, and publishes - or doesn't. Teaching
    and testing are the same act.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = Field(min_length=1, max_length=300)  # one line: what this run taught the bot
    instructions: str = Field(min_length=1)  # the full revised instructions
    description: str = ""  # a sharpened routing surface; empty ⇒ keep the current one
    cases: tuple[BotCase, ...] = Field(default=(), max_length=3)  # new evals, at most three
    # The state half of the split: operational facts and owner preferences that
    # would be WRONG baked into the shared, evaled instructions - they land on the bot's
    # memory ledger instead (personal bots immediately; a project bot's are returned for the
    # owner to add by hand, because a shared row must be typed by a person).
    memories: tuple[MemoryProposal, ...] = Field(default=(), max_length=3)


class BotConnectorAccess(BaseModel):
    """One declared connector, resolved FOR THE VIEWER: the identity, the
    author's why, MY readiness, and MY effective policy rollup. Statuses are per viewer
    because credentials are per owner - a project bot shows each member their own answer to
    "will this bot work for me"."""

    model_config = ConfigDict(frozen=True)

    slug: str
    why: str = ""
    display_name: str = ""
    catalog: bool = False  # a known catalog entry (the web renders its mark)
    status: str = "needs_connect"  # connected | needs_auth | disabled | needs_connect | custom
    connector_id: str | None = None  # MY row, when one exists - the /connectors peek link
    tools_allow: int = 0
    tools_ask: int = 0
    tools_off: int = 0
    tool_policies: dict[str, str] = Field(default_factory=dict)  # the expandable list


class BotAccess(BaseModel):
    """The access tab's payload: the connector half only - tools, skills, and can_ask_user
    already ride `BotOut`, and one wire fact should have one home."""

    model_config = ConfigDict(frozen=True)

    connectors: tuple[BotConnectorAccess, ...] = ()


class BotStats(BaseModel):
    """The bot page's stats strip: spend and runs by JOIN (never a table),
    version-blind by name; plus the two composition counts the page already owns."""

    model_config = ConfigDict(frozen=True)

    spend_usd_total: Decimal
    spend_usd_30d: Decimal
    runs: int
    last_run_at: dt.datetime | None = None
    memory_count: int = 0
    routine_count: int = 0


class SelectionCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal: str
    expected: str
    chosen: str
    ok: bool


class SelectionReport(BaseModel):
    """The verdict `publish` reads. Green is a column, not a review comment."""

    model_config = ConfigDict(frozen=True)

    bot: str
    version: int
    passed: int = 0
    total: int = 0
    cases: tuple[SelectionCaseResult, ...] = ()

    @property
    def green(self) -> bool:
        """All cases passed, and there was at least one - a bot nothing tests is not green."""
        return self.total > 0 and self.passed == self.total
