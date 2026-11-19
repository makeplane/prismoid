# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bot file format, and its validator.

A bot on disk is an **Agent Plugins v1 directory** (agent-plugins.org) - chosen deliberately, and
recorded in ADR-045. The standard defines exactly two portable component types, skills and MCP
servers, and says of everything else: *"Other proposed component types - such as commands, hooks,
agents, rules, and LSP servers - remain too client-specific for a stable portable contract."* So a
bot lives in the reverse-domain namespace the spec reserves for exactly this, and the rest of the
directory is portable to any conformant client:

    contract-review/
    ├── plugin.json                     the v1 manifest
    ├── skills/                         portable Agent Skills
    ├── mcp.json                        portable MCP servers
    └── so.plane.prismoid/
        └── bots/<name>/
            ├── BOT.md                  YAML frontmatter + instructions
            └── cases.yaml              selection eval cases - the publish gate

The cost of the shape is one small JSON file. The payoff is that a bot bundle is a thing other
clients can read half of, and a thing we can read all of.

**The doctrine is enforced here, in one place, so every door obeys it.** The CLI pushing a
directory, an HTTP push and a plugin import all come through `parse`, so a description missing
its "Not for" is a `BotError` no matter which way it arrived. The bot compiler
(`ai/agents/bot_compile.py`) asks the same `manifest_refusal` again at compile time: the
difference between an author's 422 and a stale row's honest failure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

import yaml
from pydantic import ValidationError

from prismoid.schemas.bots import (
    BOT_MODEL_CLASSES,
    BOT_TOOLS,
    MAX_BOT_CASES,
    MAX_DESCRIPTION_CHARS,
    MAX_DESCRIPTION_WORDS,
    MAX_GOLDEN_CASES,
    MAX_INSTRUCTIONS_TOKENS,
    MAX_JOB_CHARS,
    MAX_PHRASE_CHARS,
    MAX_ROUTING_PHRASES,
    MODEL_NAME_TOKENS,
    NO_BOT,
    RESERVED_BOT_NAMES,
    BotCase,
    BotManifest,
    GoldenBotCase,
    ParsedBot,
    compose_description,
)
from prismoid.schemas.ids import BotName, SkillName, skill_name
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ARTIFACT_TYPES, DEVICE_ONLY, ArtifactType
from prismoid.schemas.tokens import TOKENS
from prismoid.schemas.yaml_doc import load_untrusted

# Our namespace under the Agent Plugins spec: *"A client SHOULD base its namespace on a domain
# name it controls and SHOULD keep the namespace stable."* Changing this string orphans every
# published bundle, so it is a constant with a comment rather than a setting.
NAMESPACE: Final = "so.plane.prismoid"
BOTS_DIR: Final = "bots"
BOT_FILE: Final = "BOT.md"
CASES_FILE: Final = "cases.yaml"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)

# The Agent Skills name rule, adopted verbatim so a bot name is legal in any conformant bundle:
# 1–64 chars, lowercase alphanumerics and hyphens, no leading/trailing hyphen, no `--`.
_NAME = re.compile(r"^(?!-)(?!.*--)[a-z0-9-]{1,64}(?<!-)$")


class BotError(Exception):
    """A bot that breaks the write-time doctrine. Carries `fields` so the API can 422 with the
    exact rule broken - "invalid bot" is not a message an author can act on."""

    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


def parse(name: str, bot_md: str, cases_yaml: str = "") -> ParsedBot:
    """`BOT.md` bytes + `cases.yaml` bytes → a validated `ParsedBot`, or `BotError`."""
    if refusal := _name_refusal(name):
        raise refusal

    match = _FRONTMATTER.match(bot_md)
    if not match:
        raise BotError(
            f"{name}: BOT.md must start with a `---` YAML frontmatter block",
            {"instructions": "missing frontmatter"},
        )
    try:
        front: object = load_untrusted(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise BotError(f"{name}: the frontmatter is not valid YAML", {"frontmatter": str(exc)[:200]}) from exc
    if not isinstance(front, dict):
        raise BotError(f"{name}: the frontmatter must be a mapping", {"frontmatter": "not a mapping"})
    front = cast("dict[str, Any]", front)
    instructions = match.group(2).strip()

    declared = str(front.get("name", name)).strip()
    if declared != name:
        raise BotError(
            f"{name}: frontmatter says name: {declared!r} - it must match the directory",
            {"name": f"{declared!r} != {name!r}"},
        )

    # The structured routing keys. A verbatim `description` wins - a sentence the
    # author typed outranks fields assembled beside it - and otherwise the three keys compose the
    # same grammar through the one composer, so nobody types "Load when" by hand again.
    job = " ".join(str(front.get("job", "")).split())
    use_when = _as_phrases(front.get("use_when"))
    not_for = _as_phrases(front.get("not_for"))
    description = str(front.get("description", "")).strip()
    # The caps are door rules (a 422 naming the field), checked before the composer so its own
    # refusal never speaks first.
    if refusal := _routing_refusal(name, job, use_when, not_for):
        raise refusal
    if not description and (job or use_when or not_for):
        try:
            description = compose_description(job, use_when, not_for)
        except ValueError as exc:
            raise BotError(f"{name}: {exc}", {"description": str(exc)}) from exc

    # Empty instructions are a NEWBORN, not an error: the bot chats as the
    # identity plus an honest untaught line, and publish refuses until judgment exists.
    tokens = TOKENS.count(instructions)
    if tokens > MAX_INSTRUCTIONS_TOKENS:
        raise BotError(
            f"{name}: instructions are ~{tokens} tokens, over the {MAX_INSTRUCTIONS_TOKENS} limit - "
            "move the heavy reference into a skill and keep the instructions judgment",
            {"instructions": f"~{tokens} tokens > {MAX_INSTRUCTIONS_TOKENS}"},
        )

    model_class = _model_class(name, front.get("capability"))
    produces = _produces(name, front.get("produces"))
    tools = tuple(str(t).strip() for t in _as_list(front.get("tools")))

    connector_slugs, connector_why = _connectors(front.get("connectors"))
    fields: dict[str, Any] = {
        "name": BotName(name),
        "description": description,
        "job": job,
        "use_when": use_when,
        "not_for": not_for,
        "capability": model_class,
        "produces": produces,
        "tools": tools,
        "skills": _check_skills(name, front.get("skills")),
        "connectors": connector_slugs,
        "connector_why": connector_why,
        "can_ask_user": bool(front.get("can_ask_user", True)),
    }
    if refusal := manifest_refusal(BotManifest.model_construct(**fields)):
        raise refusal
    try:
        manifest = BotManifest(**fields)
    except ValidationError as exc:  # the belt: whatever a rule above missed still 422s by name
        raise BotError(f"{name}: {_first_error(exc)}", _error_fields(exc)) from exc
    cases, golden = _parse_cases(name, cases_yaml)
    return ParsedBot(manifest=manifest, instructions=instructions, cases=cases, golden=golden)


def parse_dir(directory: Path) -> ParsedBot:
    """Read one bot's directory - `BOT.md` plus `cases.yaml`.

    Accepts either the bot directory itself (what `bots pull` writes and `bots push` reads) or a
    plugin root containing exactly one bot, so an author can point the CLI at whichever they have
    in front of them. A plugin holding several bots is `bots_in(root)`'s job.
    """
    directory = directory.resolve()
    if not (directory / BOT_FILE).is_file():
        found = bots_in(directory)
        if len(found) == 1:
            directory = found[0]
        elif not found:
            raise BotError(
                f"{directory.name}: no {BOT_FILE} here, and no {NAMESPACE}/{BOTS_DIR}/<name>/{BOT_FILE} "
                "below it",
                {"path": "not a bot directory"},
            )
        else:
            names = ", ".join(sorted(p.name for p in found))
            raise BotError(
                f"{directory.name}: this plugin holds {len(found)} bots ({names}) - name the one you mean",
                {"path": "ambiguous"},
            )
    cases_path = directory / CASES_FILE
    return parse(
        directory.name,
        (directory / BOT_FILE).read_text(),
        cases_path.read_text() if cases_path.is_file() else "",
    )


def bots_in(plugin_root: Path) -> list[Path]:
    """Every bot directory inside an Agent Plugins bundle.

    The spec's discovery rule for skills, applied to our namespace: *"Each immediate child
    directory containing SKILL.md as a regular file is one skill. No recursive deeper searches."*
    Same shape here, so the two components are discovered the same way and neither can hide a
    file the other would have rejected.
    """
    root = plugin_root / NAMESPACE / BOTS_DIR
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir() if child.is_dir() and (child / BOT_FILE).is_file())


def _front_yaml(manifest: BotManifest) -> str:
    """A manifest → the `BOT.md` frontmatter YAML.

    `yaml.safe_dump`, never hand-formatting: a description contains `Not for: …`, and an unquoted
    YAML scalar with a `: ` in it reads back as a nested mapping. That exact bug cost the skills
    lane eight builtins; it is not being re-discovered here.
    """
    front: dict[str, Any] = {
        "name": str(manifest.name),
        "description": manifest.description,
        "capability": manifest.capability.value,
        "produces": manifest.produces.value,
    }
    # The structured keys round-trip beside the composition, so a pulled bot re-opens as the
    # meaning the author typed, not the grammar the composer wrote.
    if manifest.job:
        front["job"] = manifest.job
    if manifest.use_when:
        front["use_when"] = list(manifest.use_when)
    if manifest.not_for:
        front["not_for"] = list(manifest.not_for)
    if manifest.tools:
        front["tools"] = list(manifest.tools)
    if manifest.skills:
        front["skills"] = [str(s) for s in manifest.skills]
    if manifest.connectors:
        # A slug with a why renders as `{slug, why}`; a bare slug stays a string - both parse.
        front["connectors"] = [
            {"slug": c, "why": manifest.connector_why[c]} if c in manifest.connector_why else c
            for c in manifest.connectors
        ]
    if not manifest.can_ask_user:
        front["can_ask_user"] = False
    return yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000).strip()


def render_bot_md(manifest: BotManifest, instructions: str) -> str:
    """A manifest + instructions → the `BOT.md` bytes, the inverse of `parse`."""
    return f"---\n{_front_yaml(manifest)}\n---\n{instructions.strip()}\n"


def cases_of(
    manifest: Mapping[str, Any], *, default_expect: str = ""
) -> tuple[list[BotCase], list[GoldenBotCase]]:
    """A stored manifest payload -> its selection cases and its golden cases, the two shapes
    `render_cases` takes. ONE reader, so pull, export, templates, teach and draft can never
    disagree about what rides out (found in review: pull and export dropped golden, so a
    pull-edit-push round trip silently erased a bot's nightly cases)."""
    cases = [
        BotCase(goal=str(c["goal"]), expect=str(c.get("expect", default_expect)))
        for c in manifest.get("cases", [])
    ]
    golden = [GoldenBotCase.model_validate(g) for g in manifest.get("golden", [])]
    return cases, golden


def render_cases(cases: Sequence[BotCase], golden: Sequence[GoldenBotCase] = ()) -> str:
    """BotCases (+ golden) → `cases.yaml` - the inverse of `_parse_cases`, so a pulled bot
    round-trips with its nightly cases intact."""
    data: dict[str, Any] = {"selection": [{"goal": c.goal, "expect": c.expect} for c in cases]}
    if golden:
        data["golden"] = [
            {
                "goal": g.goal,
                **({"must_contain": list(g.must_contain)} if g.must_contain else {}),
                **({"inputs": list(g.inputs)} if g.inputs else {}),
            }
            for g in golden
        ]
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)


# ── the rules, one function each so a 422 can name exactly one ────────────────────────────────


def manifest_refusal(manifest: BotManifest) -> BotError | None:
    """The first rule `manifest` breaks, or None: the door's 422 and the compiler's refusal of a
    stale row read this one list."""
    name = str(manifest.name)
    return (
        _name_refusal(name)
        or _routing_refusal(name, manifest.job, manifest.use_when, manifest.not_for)
        or _description_refusal(name, manifest.description, manifest.use_when, manifest.not_for)
        or _capability_refusal(name, manifest.capability)
        or _produces_refusal(name, manifest.produces)
        or _tools_refusal(name, manifest.tools, manifest.produces)
    )


def _name_refusal(name: str) -> BotError | None:
    if not _NAME.fullmatch(name):
        return BotError(
            f"{name!r}: a bot name is 1–64 lowercase letters, digits and single hyphens, not "
            "starting or ending with one (the Agent Skills name rule, so a bundle stays portable)",
            {"name": "malformed"},
        )
    if name in RESERVED_BOT_NAMES:
        return BotError(reserved(name), {"name": "reserved"})
    if any(token in name.lower() for token in MODEL_NAME_TOKENS):
        return BotError(
            f"{name!r} names a model. A bot is a role - 'contract-reviewer', not 'claude-reviewer' - "
            "because what model serves it is a routing decision made at run time",
            {"name": "names a model"},
        )
    return None


def _routing_refusal(name: str, job: str, use_when: Sequence[str], not_for: Sequence[str]) -> BotError | None:
    """The structured keys' caps: a verbatim `description` skips the composer, and the manifest's
    own caps would then surface as a bare pydantic error the author cannot act on."""
    if len(job) > MAX_JOB_CHARS:
        return BotError(
            f"{name}: job is {len(job)} characters, over the {MAX_JOB_CHARS} limit - one sentence",
            {"job": f"{len(job)} chars > {MAX_JOB_CHARS}"},
        )
    for key, phrases in (("use_when", use_when), ("not_for", not_for)):
        if len(phrases) > MAX_ROUTING_PHRASES:
            return BotError(
                f"{name}: at most {MAX_ROUTING_PHRASES} {key} phrases - keep the surface tight",
                {key: f"{len(phrases)} phrases > {MAX_ROUTING_PHRASES}"},
            )
        for phrase in phrases:
            if len(phrase) > MAX_PHRASE_CHARS:
                return BotError(
                    f"{name}: keep each {key} phrase under {MAX_PHRASE_CHARS} characters",
                    {key: f"phrase {len(phrase)} chars > {MAX_PHRASE_CHARS}"},
                )
    return None


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "the manifest is invalid"
    first = errors[0]
    field = ".".join(str(p) for p in first.get("loc", ())) or "manifest"
    return f"{field}: {first.get('msg', 'invalid')}"


def _error_fields(exc: ValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        key = str(loc[0]) if loc else "manifest"
        fields.setdefault(key, str(err.get("msg", "invalid"))[:200])
    return fields


def reserved(name: str) -> str:
    """The refusal of a name Prismoid keeps for itself, with a name the author can take instead."""
    return f"{name!r} is reserved for Prismoid's own agents. Pick another name, for example '{name}-helper'."


def bounded(description: str, not_for: Sequence[str]) -> bool:
    """Whether a written description says what the bot is not for: its structured `not_for`, in any
    language, else the boundary written into verbatim prose."""
    return bool(not_for) or "not for" in description.lower()


# A description this short cannot say what a bot is for and what it is not.
_MIN_DESCRIPTION_CHARS: Final = 21


def _description_refusal(
    name: str, description: str, use_when: Sequence[str], not_for: Sequence[str]
) -> BotError | None:
    """The grammar on whatever IS written: empty stays legal on a draft, and publish demands it."""
    if not description:
        return None
    tight = "the Lead reads only this to choose between your bots, so it must be tight"
    if len(description) > MAX_DESCRIPTION_CHARS:
        return BotError(
            f"{name}: description is {len(description)} characters, over the {MAX_DESCRIPTION_CHARS} "
            f"limit - {tight}",
            {"description": f"{len(description)} chars > {MAX_DESCRIPTION_CHARS}"},
        )
    words = len(description.split())
    if words > MAX_DESCRIPTION_WORDS:
        return BotError(
            f"{name}: description is {words} words, over the {MAX_DESCRIPTION_WORDS} limit - {tight}",
            {"description": f"{words} words > {MAX_DESCRIPTION_WORDS}"},
        )
    if len(description) < _MIN_DESCRIPTION_CHARS:
        return BotError(
            f"{name}: description is {len(description)} characters - say what the bot is for, when "
            "to use it and what it is not for",
            {"description": f"{len(description)} chars < {_MIN_DESCRIPTION_CHARS}"},
        )
    if not use_when and "load when" not in description.lower():
        return BotError(
            f"{name}: add at least one use_when phrase (or a 'Load when…' in the description) - "
            "that is what the Lead matches on",
            {"description": "missing 'Load when'"},
        )
    if not bounded(description, not_for):
        return BotError(
            f"{name}: add at least one not_for phrase (or a 'Not for…' in the description) - the "
            "boundary is what stops a bot grabbing work that is not its own",
            {"description": "missing 'Not for'"},
        )
    return None


def _check_skills(name: str, raw: object) -> tuple[SkillName, ...]:
    """The pinned skills, each a legal skill name, or a `BotError` on the skills field."""
    try:
        return tuple(skill_name(str(entry).strip()) for entry in _as_list(raw))
    except ValueError as exc:
        raise BotError(f"{name}: skill pin {exc}", {"skills": "not a legal skill name"}) from exc


def _model_class(name: str, raw: object) -> ModelClass:
    value = str(raw or ModelClass.FAST_EXECUTOR.value).strip()
    try:
        return ModelClass(value)
    except ValueError:
        allowed = ", ".join(sorted(c.value for c in BOT_MODEL_CLASSES))
        raise BotError(
            f"{name}: {value!r} is not a model class. Pick one of: {allowed}",
            {"capability": "unknown"},
        ) from None


def _capability_refusal(name: str, model_class: ModelClass) -> BotError | None:
    if model_class in BOT_MODEL_CLASSES:
        return None
    allowed = ", ".join(sorted(c.value for c in BOT_MODEL_CLASSES))
    return BotError(
        f"{name}: {model_class.value!r} is lane-reserved and not available to a bot - verification "
        f"integrity is not a dial, and image models are served by a tool. Pick one of: {allowed}",
        {"capability": "reserved"},
    )


def _produces(name: str, raw: object) -> ArtifactType:
    value = str(raw or ArtifactType.DOCUMENT.value).strip()
    try:
        return ArtifactType(value)
    except ValueError:
        raise BotError(f"{name}: {value!r} is not an artifact kind", {"produces": "unknown"}) from None


def _produces_refusal(name: str, produces: ArtifactType) -> BotError | None:
    if produces in DEVICE_ONLY:  # no plan types a step with it: the device's maker publishes it
        return BotError(f"{name}: {produces.value!r} is not an artifact kind", {"produces": "unknown"})
    if produces not in ARTIFACT_TYPES:
        return BotError(
            f"{name}: {produces.value!r} is a legacy wire value, not a publishable kind",
            {"produces": "not publishable"},
        )
    return None


def _tools_refusal(name: str, tools: Sequence[str], produces: ArtifactType) -> BotError | None:
    unknown = sorted(set(tools) - BOT_TOOLS)
    if unknown:
        allowed = ", ".join(sorted(BOT_TOOLS))
        return BotError(
            f"{name}: unknown tool grant(s) {', '.join(unknown)}. A bot may hold: {allowed}",
            {"tools": ", ".join(unknown)},
        )
    # A bot without the grant gets the text lane: its run's output IS the document and the
    # SYSTEM publishes it, and that lane only makes sense for prose.
    if "frame.publish" not in tools and produces is not ArtifactType.DOCUMENT:
        return BotError(
            f"{name}: a bot that produces {produces.value} must hold 'frame.publish' - only a "
            "document can be the run's own output",
            {"tools": "missing frame.publish"},
        )
    return None


def _parse_cases(name: str, cases_yaml: str) -> tuple[tuple[BotCase, ...], tuple[GoldenBotCase, ...]]:
    """The selection cases (the publish gate) and the optional `golden:` cases (the advisory
    nightly lane `evals/cases/bot_golden.py` reads). `expect` must be this bot or `none`."""
    raw: list[Any] = []
    raw_golden: list[Any] = []
    if cases_yaml.strip():
        try:
            data: object = load_untrusted(cases_yaml) or {}
        except yaml.YAMLError as exc:
            raise BotError(f"{name}: cases.yaml is not valid YAML", {"cases": str(exc)[:200]}) from exc
        doc = cast("dict[str, Any]", data) if isinstance(data, dict) else {}
        raw = doc.get("selection", []) if isinstance(data, dict) else []
        raw_golden = doc.get("golden", []) if isinstance(data, dict) else []

    cases: list[BotCase] = []
    for entry in raw:
        if not isinstance(entry, dict) or "goal" not in entry:
            continue
        entry = cast("dict[str, Any]", entry)
        expect = str(entry.get("expect", name)).strip()
        if expect not in (name, NO_BOT):
            raise BotError(
                f"{name}: case expects {expect!r} - a bot's own cases may only expect {name!r} or "
                f"{NO_BOT!r} (the negative case is how precision is measured)",
                {"cases": f"expect={expect}"},
            )
        cases.append(BotCase(goal=str(entry["goal"]), expect=expect))

    if len(cases) > MAX_BOT_CASES:
        raise BotError(
            f"{name}: {len(cases)} cases is over the {MAX_BOT_CASES} limit - the eval must stay "
            "sub-minute or the edit loop dies",
            {"cases": f"{len(cases)} > {MAX_BOT_CASES}"},
        )

    golden: list[GoldenBotCase] = []
    for entry in raw_golden:
        if not isinstance(entry, dict):
            continue
        entry = cast("dict[str, Any]", entry)
        if not str(entry.get("goal", "")).strip():
            continue
        golden.append(
            GoldenBotCase(
                goal=str(entry["goal"]).strip(),
                must_contain=tuple(str(m) for m in _as_list(entry.get("must_contain"))),
                inputs=tuple(str(i) for i in _as_list(entry.get("inputs"))),
            )
        )
    if len(golden) > MAX_GOLDEN_CASES:
        raise BotError(
            f"{name}: {len(golden)} golden cases is over the {MAX_GOLDEN_CASES} limit - these run "
            "real end-to-end tasks nightly, and five is already real money",
            {"cases": f"golden {len(golden)} > {MAX_GOLDEN_CASES}"},
        )
    return tuple(cases), tuple(golden)


def _connectors(raw: object) -> tuple[tuple[str, ...], dict[str, str]]:
    """The declared connector slugs, each once, and the author's reason per slug: an entry is a
    slug, or `{slug, why}`."""
    slugs: list[str] = []
    why_of: dict[str, str] = {}
    for entry in _as_list(raw):
        if isinstance(entry, dict):
            entry = cast("dict[str, Any]", entry)
            slug = str(entry.get("slug", "")).strip()
            why = str(entry.get("why", "")).strip()
        else:
            slug, why = str(entry).strip(), ""
        if slug and slug not in slugs:
            slugs.append(slug)
            if why:
                why_of[slug] = why[:200]
    return tuple(slugs), why_of


def _as_phrases(raw: object) -> tuple[str, ...]:
    """The routing phrase lists (`use_when` / `not_for`): a YAML list is one phrase per item, a
    scalar string is ONE phrase. `_as_list` splits a scalar on whitespace, which turned
    `use_when: a contract needs review` into four one-word triggers (found in review)."""
    if raw is None:
        return ()
    if isinstance(raw, list):
        items: list[object] = list(raw)  # pyright: ignore[reportUnknownArgumentType]
    else:
        items = [raw]
    return tuple(text for p in items if (text := " ".join(str(p).split())))


def _as_list(raw: object) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part for part in re.split(r"[,\s]+", raw.strip()) if part]
    if isinstance(raw, list):
        return list(raw)  # pyright: ignore[reportUnknownArgumentType]
    return [raw]
