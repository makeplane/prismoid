# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The handlers `execute_step` dispatches to, one module per outcome of a rung.

entry.py     - the door: an ended task, a settled twin, a replay, a park's gate, a resume, a crash
recover.py   - what the door does for a step a crash, a park or a person left mid-flight
rung.py      - the top of a fresh rung: the doors before a run is bought, the steers taken
deferral.py  - a run that stopped without ending: one handler per park kind
failure.py   - an attempt that raised
fence.py     - fence 1 and the typed ending
expand.py    - a lane step's result fanning the plan out
published.py - the published lane: the fleet's and QA's in-line reviews, then done
produce.py   - the produce ladder: nothing published, retry or escalate
state.py     - the `Ladder` a step walks, the `Ran` a rung made, and `AGAIN`
"""
