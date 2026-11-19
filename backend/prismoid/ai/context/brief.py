# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The step brief: an attempt's contract, inputs, connectors and last failures as the prompt it reads.
An agent not told a file exists will not read it."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from prismoid.ai.context.inline import INCLUDED
from prismoid.ai.frames.layout import ANSWERS, CONTEXT, INSTRUCTIONS, THREAD
from prismoid.ai.tools.ask import SUBJECT_IS_NOT_A_SERVICE
from prismoid.schemas.connectors import ConnectorSpec
from prismoid.schemas.contract import Contract
from prismoid.schemas.egress import fenced
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepSpec

REQUEST_LEAD = "Your goal serves what the person asked; their message, with anything they pasted, is below."
REQUEST_TITLE = "the person's request"
REQUEST_FRAMING = "their words, verbatim"
HANDOVER = (
    "## Your deliverable\n\nOne {kind}: build it in `{where}` and publish it once with the publish tool as "
    "type='{kind}'. When your role says the system publishes what you submit, submit it instead."
)


def prompt_for(
    spec: StepSpec,
    gotchas: list[str] | None = None,
    connectors: Sequence[ConnectorSpec] = (),
    *,
    asker: bool = False,
    guarded: bool = False,
    interruption: str | None = None,
    still_running: bool = False,
    exit_code: int | None = None,
    inlined: Collection[str] = (),
    revising: Collection[str] = (),
    today: str = "",
) -> str:
    """The contract as a prompt (criteria, inputs, deliverables to revise, prior failures, the request,
    today), so the agent never guesses what it is judged on."""
    contract = spec.contract
    assert contract is not None
    lines = [contract.goal, ""]
    if spec.request.strip() and spec.request.strip() not in contract.goal:
        lines += [REQUEST_LEAD, fenced(REQUEST_TITLE, REQUEST_FRAMING, spec.request.strip()), ""]

    # **The answers are instructions; the sources are evidence.** They arrive in the same directory
    # and they are emphatically not the same kind of thing. A file that tells you the reader is the
    # board is not a file you cite for a fact about the board - and an agent handed both under one
    # heading ("everything you assert must come from these files") will eventually cite the second
    # as though it were the first. The whole point is that an answer *shapes* the work.
    # The context file gets the same wall: never a source, whatever part of it instructs.
    sources = [p for p in contract.inputs if p not in INSTRUCTIONS]

    if ANSWERS in contract.inputs:
        lines.append(
            f"The user was asked some questions before this started, and answered them. Read "
            f"`{ANSWERS}` first - it is what they told you, question and answer. It is an "
            f"**instruction**, not a source: it shapes what you write, and you do not cite it. "
            "Use every answer: each one is a requirement your work must show."
        )
        lines.append("")

    if CONTEXT in contract.inputs:
        lines.append(
            f"`{CONTEXT}` is what the system remembers about this user, their project, and this "
            f"thread: its project instructions and skill notes are instructions, its memories are "
            f"fenced data that shape what you produce. It is never a source: never cite it, quote "
            f"it, or list it as provenance."
        )
        lines.append("")

    if THREAD in contract.inputs:
        lines.append(
            f"`{THREAD}` is the conversation that led to this task - every earlier turn, what "
            f'was asked and what was delivered. Read it so "that memo" and "the same format" '
            f"mean what the user means, and build on the earlier work instead of redoing it. It "
            f"is an **instruction**, not a source: never cite it, quote it, or list it as "
            f"provenance."
        )
        lines.append("")

    if prior := [path for path in sources if path in revising]:
        listed = ", ".join(f"`{path}`" for path in prior)
        lines.append(
            f"Your earlier deliverables in this thread: {listed}. When the request changes one, revise "
            "it under the same file name: keep everything the request does not change, and search or "
            "browse again only for what it changes."
        )
        lines.append("")

    if spec.skill_pins:
        # Skills are materialised into the frame either way; being on disk is not the same as
        # being known about (the same sentence that justifies naming the sources).
        skills = [f"/skills/{pin.name}/SKILL.md" for pin in spec.skill_pins]
        listed = ", ".join(f"`{p}` {INCLUDED}" if p in inlined else f"`{p}`" for p in skills)
        lines.append(
            f"You have skills loaded - follow what each says: {listed}."
            if all(p in inlined for p in skills)
            else f"You have skills loaded - read each before you start, and follow what it says: {listed}."
        )
        lines.append("")

    if connectors:
        # Connector tools are in the toolset either way; the same being-known-about sentence
        # applies. The prefix is named so "use gmail" and `gmail_*` mean the same thing.
        listed = ", ".join(f"{spec_.name} (tools prefixed `{spec_.slug}_`)" for spec_ in connectors)
        lines.append(
            f"You have connectors mounted - live external services you may call directly through "
            f"their tools: {listed}. Prefer them over guessing about anything they can answer. "
            "When the goal is an ACTION on one of these services (create the page, send the "
            "mail, file the ticket), PERFORM it through the tools - never deliver instructions "
            "for the user to do it by hand; the side effect is the deliverable. "
            "Some tools pause for the user's approval before running - that pause is normal, "
            "not an error; if an action is denied, continue without it and say what was skipped."
            + (
                " If the goal needs a DIFFERENT service than these, request_connector(service, "
                "why) asks the user to connect it right in the conversation."
                if asker
                else ""
            )
        )
        lines.append("")
    elif asker:
        # No reach mounted: the connect block replaces the dead-end sentence. Only
        # for agents that HOLD the tool (the ask grant) - teaching it to a fan-out seat would
        # be a call into a wall.
        lines.append(
            "No external service connectors are mounted on this task. If the goal needs the "
            "user's OWN account on a service - to read their data or act in it (their Notion "
            'pages, their mail, their tracker) - or tells you to use one ("ask deepwiki"), '
            "call request_connector(service, why) FIRST: answering from memory instead is not "
            f"doing the task. {SUBJECT_IS_NOT_A_SERVICE} The user gets a "
            "connect block right in the conversation, can authorize on the spot, and you "
            "continue with the service's tools available - or they skip and you continue "
            "without, saying what was skipped. Never pretend an action on an unconnected "
            "service happened."
        )
        lines.append("")

    if sources:
        unread = [path for path in sources if path not in inlined]
        lines.append(
            "Your sources are already in the sandbox. Read them before you write:"
            if len(unread) == len(sources)
            else "Your sources are included in full below:"
            if not unread
            else "Your sources are in the sandbox, and the ones marked are included in full below:"
        )
        lines += [f"- {path} {INCLUDED}" if path in inlined else f"- {path}" for path in sources]
        reading = (
            ""
            if not unread
            else "Read them with read_file. "
            if len(unread) == len(sources)
            else "Read the others with read_file. "
        )
        lines.append(
            f"Everything you assert must come from these files. {reading}Do not "
            "invent a number, a quotation, a date or a citation - a verifier re-derives every one "
            "of them from exactly these bytes, and anything it cannot find fails the artifact."
        )
        lines.append("")

    lines += _owed(contract)

    if gotchas and guarded:
        # An action step has nothing to "publish again", and its last attempt
        # may have changed the world: say what stopped it and that its effects may already stand.
        lines += [
            "",
            "---",
            "",
            f"## Your previous attempt stopped (this is attempt {len(gotchas) + 1})",
            "",
            *gotchas,
            "",
            "Its conversation could not be carried over, but what it did may already have taken "
            "effect. Check the page or files first; do not redo actions that already took effect.",
        ]
    elif gotchas:
        lines += [
            "",
            "---",
            "",
            f"## Your previous attempt did not finish (this is attempt {len(gotchas) + 1})",
            "",
            "What went wrong:",
            "",
            *gotchas,
            "",
            "**Fix exactly what is named above.** If a figure cannot be found in the sources, either",
            "derive it and show the working, or remove the claim: a sentence you cannot support is",
            "worth less than no sentence. Then write the file and publish it again.",
        ]

    if interruption is not None:
        lines += ["", interruption_note(interruption, still_running=still_running, exit_code=exit_code)]

    if today:
        lines.append(today)

    return "\n".join(lines)


def _owed(contract: Contract) -> list[str]:
    """What the step owes: its file and how it is handed over, then what the work is judged on, then notes."""
    lines = [handover(contract.output), ""] if contract.output is not None else []
    if contract.acceptance:
        lines += ["You will be judged on:", *(f"- {a}" for a in contract.acceptance)]
    return [*lines, "", contract.notes] if contract.notes else lines


def handover(output: OutputRef) -> str:
    """How the step's file is handed over: the agent's instruction, never a line its work is judged on."""
    where = "/out/change" if output.type is ArtifactType.CHANGE else "/work"
    return HANDOVER.format(kind=output.type.value, where=where)


INTERRUPTED_NOTE = (
    "Your last command was cut off: `{head}`. Check the folder (git status, the files you touched) "
    "before you act."
)
STILL_RUNNING_NOTE = (
    "Your last command was still running when Prismoid recovered: `{head}`. Check whether it finished "
    "(git status, the files it touched) before you act."
)
UNSEEN_NOTE = (
    "Your last command finished while Prismoid was away, and you never saw its result (exit "
    "{exit_code}): `{head}`. Check what it did (git status, the files it touched) before you act, "
    "and do not run it again to find out."
)


def interruption_note(head: str, *, still_running: bool = False, exit_code: int | None = None) -> str:
    """What the attempt a person resumed on the device is told about the command a crash cut off -
    or left running, or that finished unseen, when only the worker went away."""
    if exit_code is not None:
        return UNSEEN_NOTE.format(head=head, exit_code=exit_code)
    return (STILL_RUNNING_NOTE if still_running else INTERRUPTED_NOTE).format(head=head)
