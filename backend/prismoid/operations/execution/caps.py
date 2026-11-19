# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step's gate budgets and the words a run is answered with when one is spent, or when a call
shares its turn with a park. Past a budget the tool answers itself and the run continues in place:
a step that cannot stop asking never finishes.
"""

from prismoid.ai.tools.call_rows import DECLINED

# **The ask budget**. A step may park on a question gate at most this many
# times; `ask_user` batches now, so one gate carries everything it needs, and a second gate is a
# legitimate follow-up. Past the budget the tool ANSWERS ITSELF with `OUT_OF_QUESTIONS` and the
# run continues in place - because the alternative was observed live: fifteen "One question
# before I continue" gates, serially, on one task. A step that cannot stop asking never finishes,
# and the human it interrogates leaves.
MAX_STEP_QUESTION_GATES = 2
OUT_OF_QUESTIONS = (
    "No more questions are available for this task - the question budget is spent. Proceed with "
    "your best judgment, choose sensible defaults, and state the assumptions you made in the "
    "deliverable."
)

# **The approval budget**. Deliberately generous next to the ask cap: an approval is
# the product WORKING (a side-effectful connector tool pausing for consent), where a question
# is the product stalling. Past it the overflow default is DENY - never silently run the side
# effect the policy said to ask about - and the run continues in place, told so.
MAX_STEP_APPROVAL_GATES = 8
DENIED_BY_USER = DECLINED  # a person's Deny: an answer the run continues on, never asked again
OUT_OF_APPROVALS = (
    "not approved: this step's approval budget is spent - continue without this action and say "
    "what was skipped"
)
# A browser acting call's approval could not be matched back to the call it was granted for
# (id AND model-facing tool name both, the binding rule) - never fall back to an
# expect-only override, which is not a valid call and would be refused by the toolset anyway
# for missing required args. Denied outright, with a reason the model can act on.
APPROVAL_UNMATCHED = "approval could not be matched to its call; take a new snapshot and ask again"

# **The connect budget**. A step may raise the in-conversation connect block at most
# this many times; past it the tool answers itself and the run continues - a step that keeps
# asking for services never finishes.
MAX_STEP_CONNECT_GATES = 2
OUT_OF_CONNECTS = (
    "no more connection requests are available for this step - continue without the service "
    "and say what was skipped"
)
ONE_CONNECT_AT_A_TIME = (
    "not raised: one connection request at a time - if this service is still needed after the "
    "pending one resolves, request it again"
)
ONE_BATCH_AT_A_TIME = (
    "not asked: one ask_user batch at a time - fold every question into a single call; if "
    "these still matter after the pending batch resolves, ask them again in ONE batch"
)
RETRY_AFTER_ANSWER = (
    "not run: a question to the user was pending in the same turn - if this action is still "
    "needed after the answer arrives, call it again"
)

# **The handoff budget**. `browser.handoff` parks the step on a person; past this many
# in one step the verb answers itself and the run continues in place - a step that keeps asking for
# a person never finishes. A person TAKING control is not budgeted: it is not the agent's spend.
MAX_STEP_HANDOFF_GATES = 3
OUT_OF_HANDOFFS = "You have asked for a person three times in this step. Stop and say what you need."
# The other pending ids of a turn whose `browser_handoff` came back `OUT_OF_HANDOFFS`: nobody took
# the browser, so they were not run for that - they shared the turn with a refused handoff.
NOT_RUN_BESIDE_HANDOFF = (
    "not run: it shared a turn with a browser_handoff past this step's handoff budget - call it "
    "again if it is still needed"
)
# The parked call's result (verbatim): who drove and for how long, or why nobody did.
CONTROL_TAKEN_OVER = (
    "{name} drove the browser for {d} and handed back control. The page may have changed: take a "
    "new snapshot before acting."
)
CONTROL_HANDED_BACK = (
    "{name} took over and handed back after {d}. Take a new snapshot to see where things stand."
)
CONTROL_NOBODY = "Nobody took over ({why}). Continue without it if you can, or stop and say what is needed."
