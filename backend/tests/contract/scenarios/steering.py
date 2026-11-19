# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A person's hands on a running turn, declared as data: a steer taken at a safe point, a steer that
goes round again or is refused, a steer withdrawn or stale, a pause and its resume, a cancel. See
`all.py` for how a scenario is added and `harness/people.py` for when the person acts."""

from __future__ import annotations

from tests.contract.harness.moves_lanes import follow_up
from tests.contract.harness.people import (
    Edge,
    Stage,
    cancels,
    meanwhile,
    pauses,
    resumes,
    retracts,
    steers,
    together,
)
from tests.contract.harness.people import Who as By
from tests.contract.harness.rig import Scenario
from tests.contract.harness.voices import calls, leads, publishes, revises, step, writes

ASK = "Write me a memo on the Eddington correction"
LOOK = calls("read_file", path="/work/notes.md")  # a call to hold: chosen before the steer, never run
EUROS = steers("write it in euros")

SAFE_POINTS = [
    # The steer lands while the writer is choosing its first call: that call is held, not run, and
    # the next request carries the steer.
    Scenario(
        "steer_before_tool",
        ASK,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (meanwhile(EUROS, LOOK), writes("The memo, in euros.")),
        },
    ),
    # The steer lands while the writer is writing its last words: the ending turns into one more request.
    Scenario(
        "steer_final_answer",
        ASK,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (meanwhile(EUROS, writes("The memo, in dollars.")), writes("The memo, in euros.")),
        },
    ),
    # The steer lands after the plan was drafted and before its first step: the unstarted step
    # takes it in its contract, and the next wave is re-drafted under it.
    Scenario(
        "steer_replans_unstarted",
        ASK,
        {
            "lead": (
                leads(step("s1", "writer"), step("s2", "writer", "s1")),
                revises(step("s2", "writer")),
            ),
            "writer": (meanwhile(EUROS, LOOK), writes("Notes, in euros."), writes("The memo, in euros.")),
        },
    ),
    # The steer lands while the Lead is deciding: its ending is held and one more request, told the
    # steer, ends the turn instead.
    Scenario(
        "steer_while_planning",
        ASK,
        {
            "lead": (meanwhile(EUROS, leads(step("s1", "writer"))), leads(step("s1", "writer"))),
            "writer": (writes("The memo, in euros."),),
        },
    ),
]

FENCES = [
    # The steer lands after the step published and before it settled: its output is superseded and
    # the step goes again in place, on the same attempt number.
    Scenario(
        "steer_reruns_in_place",
        ASK,
        {"lead": (leads(step("s1", "writer")),), "writer": (writes("In dollars."), writes("In euros."))},
        stage=Stage(at={Edge.HARVEST: (EUROS,)}),
    ),
    # The steer lands after the last step's workflow ended and before delivery: nothing delivers
    # until a sink the engine re-drafts in code (the Lead is not asked again) has run.
    Scenario(
        "steer_holds_delivery",
        ASK,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (writes("In dollars."), writes("In euros.")),
        },
        stage=Stage(at={Edge.JOIN: (together(), EUROS)}),  # the Lead's step joins first: nothing then
    ),
]

WITHDRAWN = [
    # The author withdraws the steer before any step took it: it never applies and the step finishes.
    Scenario(
        "steer_retracted",
        ASK,
        {"lead": (leads(step("s1", "writer")),), "writer": (writes("In dollars."),)},
        stage=Stage(at={Edge.HARVEST: (together(EUROS, retracts()),)}),
    ),
    # A teammate steers; the owner, whose screen showed an older epoch, is told what she missed.
    # Her own later steers are not news to her, and a steer at the current epoch goes through.
    Scenario(
        "steer_stale",
        ASK,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (
                meanwhile(
                    together(
                        steers("write it in euros", by=By.TEAMMATE, saw=1),
                        steers("keep it short", saw=1),
                        steers("keep it short", saw=0),
                        steers("keep it short", saw=2),
                        steers("and cite the paper", saw=2),
                    ),
                    LOOK,
                ),
                writes("A short memo, in euros."),
            ),
        },
        stage=Stage(teammate=True),
    ),
]

ACTED = [
    # The step ran a shell command in a frame with a route out, so it may have acted in the world: a
    # steer that arrives as it finishes is never answered by running it again. Delivery refuses it.
    Scenario(
        "steer_after_acting_is_refused",
        "Write me a script that posts the weekly numbers",
        {"maker": (calls("shell", command="curl -s https://api.example.com"), *publishes("tool.py", "code"))},
        {"deliverable": "code"},
        stage=Stage(route_out=True, at={Edge.HARVEST: (EUROS,)}),
    ),
]

TURNS = [
    # The second turn's start moved the epoch on: a steer from a screen that showed the first turn's
    # is stale and names the turn, and one sent at the current epoch goes through.
    Scenario(
        "steer_across_turns",
        ASK,
        {
            "lead": (leads(step("s1", "writer")), leads(step("s1", "writer"))),
            "writer": (
                writes("The memo."),
                meanwhile(together(steers("shorter", saw=1), steers("shorter", saw=2)), LOOK),
                writes("The memo, shorter."),
            ),
        },
        then=(follow_up("Now cut it to one page"),),
    ),
]

PAUSES = [
    # The pause lands while the writer is choosing a call: the call is deferred, the step parks on a
    # question-less gate, and the resume wakes it to finish.
    Scenario(
        "pause_parks_the_step",
        ASK,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (meanwhile(pauses(), LOOK), writes("The memo.")),
        },
        stage=Stage(at={Edge.PARK: (resumes(),)}),
    ),
    # The pause lands while the writer is writing its last words, with no call left to park on: the
    # step finishes and the frontier holds the next step until the resume.
    Scenario(
        "pause_holds_the_frontier",
        ASK,
        {
            "lead": (leads(step("s1", "writer"), step("s2", "writer", "s1")),),
            "writer": (meanwhile(pauses(), writes("Notes.")), writes("The memo.")),
        },
        stage=Stage(at={Edge.PARK: (resumes(),)}),
    ),
    # The same pause landing in the last step: nothing delivers while the task is paused.
    Scenario(
        "pause_holds_delivery",
        ASK,
        {"lead": (leads(step("s1", "writer")),), "writer": (meanwhile(pauses(), writes("The memo.")),)},
        stage=Stage(at={Edge.PARK: (resumes(),)}),
    ),
    # A steer sent while the step is parked on the pause is taken when the run resumes.
    Scenario(
        "steer_while_paused",
        ASK,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (meanwhile(pauses(), LOOK), writes("The memo, in euros.")),
        },
        stage=Stage(at={Edge.PARK: (together(EUROS, resumes()),)}),
    ),
]

CANCELS = [
    # The cancel lands while the writer is choosing a call: the call never runs and the turn ends
    # cancelled, the only hard stop.
    Scenario(
        "cancel_stops_the_step",
        ASK,
        {"lead": (leads(step("s1", "writer")),), "writer": (meanwhile(cancels(), LOOK),)},
    ),
    # A steer nothing took before the cancel is refused by the rule, with the cancel as the reason.
    Scenario(
        "cancel_refuses_the_steer",
        ASK,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (meanwhile(together(EUROS, cancels()), LOOK),),
        },
    ),
    # Cancelled while a step waits on its pause: the gate closes as a cancel and the step ends.
    Scenario(
        "cancel_while_paused",
        ASK,
        {"lead": (leads(step("s1", "writer")),), "writer": (meanwhile(pauses(), LOOK),)},
        stage=Stage(at={Edge.PARK: (cancels(),)}),
    ),
    # Cancelled while the frontier holds a pause: the held turn ends cancelled.
    Scenario(
        "cancel_while_frontier_held",
        ASK,
        {
            "lead": (leads(step("s1", "writer"), step("s2", "writer", "s1")),),
            "writer": (meanwhile(pauses(), writes("Notes.")),),
        },
        stage=Stage(at={Edge.PARK: (cancels(),)}),
    ),
]

STEERING: list[Scenario] = [*SAFE_POINTS, *FENCES, *WITHDRAWN, *ACTED, *TURNS, *PAUSES, *CANCELS]
