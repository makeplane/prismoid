# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The contract harness: one real turn of the engine per scenario, its durable record held to a
recorded shape, so a refactor that claims to change no behaviour can prove it. Read CLAUDE.md in
this folder before adding to it.

    harness/voices.py       scripted models: `says`, `submits`, `calls`, `publishes`, `planned`
    harness/rig.py          `Scenario` and `run_turn`: the real `run_task` body with only the edges faked
    harness/people.py       a person acting on the running turn (steer, pause, cancel), and when
    harness/answers.py      a gate answered as data: accepts, declines, cancels_gate, lapses (the clock)
    harness/moves_lanes.py  what happens around the first turn: a follow-up, a bot, a judge, the receipts
    harness/world.py        what a scenario fakes beside its models: a request's price, a service
    harness/resume.py       a parked turn frozen, its rows kept, put back and resumed
    harness/shape.py        the record reduced to structure, and the comparison
    scenarios/all.py        the scenarios, as data: a reply, an escalation, a plan, and every family
    scenarios/steering.py   a person's hands on the turn: steers, pauses, cancels
    scenarios/gates.py      the gates: a question, an approval, a connect, the budget; the resume fixtures
    scenarios/lanes.py      around a turn: a follow-up reply, a bot's direct line, the receipt
    fixtures/<name>.json    the parked rows and the shape their resume wrote
    shapes/<name>.json      what each scenario's record looked like when it was recorded
    test_event_shapes.py    every scenario against its recorded file
    test_gate_resume.py     every fixture resumed against its recorded shape
    test_shape_rules.py     the mechanism itself, and the proof that a recorded file can fail
"""
