# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The typed ending's and the run guard's words: every sentence the
model or a person reads about an Outcome, a proof, a budget park or a stall. Pure: no clock, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from prismoid.schemas.outputs import Outcome
from prismoid.schemas.plan import BudgetFigures, said_usd
from prismoid.schemas.verification import Diagnosis

BLOCKED_WHY: Final[dict[str | None, str]] = {
    "login": "sign-in needed",
    "approval": "approval needed",
    "person": "a person is needed",
    "budget": "budget reached",
    None: "blocked",
}

HANDOFF_NUDGE: Final = (
    "You finished as blocked on a sign-in or on something only a person can do. A person can take "
    "over this browser: call `browser_handoff` with one line saying what they need to do, then "
    "continue on the page they leave you."
)
# A hand-off already came back in this run (the person skipped it, nobody came, or they handed back):
# they had their turn, so a second hand-off is never the nudge. Another way the goal names is.
HANDED_OVER_NUDGE: Final = (
    "You finished as blocked, and the person already had the browser handed to them in this step. "
    "Do not hand it over again. If the goal or the person named another source, site or way to get "
    "this, use it now. Otherwise finish with `done` again, saying exactly what is still needed."
)
# A person's Deny is their answer: the step goes on without that action and ends on what it did.
DECLINED_NUDGE: Final = (
    "You finished as blocked on an approval the person declined. Their no is an answer, not a wall: "
    "finish with `done` again, `status='partial'` (or `'done'` if the goal did not need that action), "
    "saying what you did and what was skipped because they declined."
)

# A computer that ended failed or blocked before it opened a page or ran a command gave up on a
# guess: it is told to drive first, once. A refusal, and a stop on the budget, an approval or a
# person, is a decision and is never told this.
DRIVE_FIRST: Final = (
    "You finished before you opened the site or ran anything. Drive first: `browser_open` the site "
    "the task names (a new site asks the person, which is normal) and get as far as the goal's own "
    "details take you - search, filter, pick. At a sign-in or a payment, hand the browser to the "
    "person with `browser_handoff`. Then finish with `done` again."
)


def proof_nudge(reason: str) -> str:
    return (
        f"Your `done` could not be confirmed: {reason}. Check it now - look at the page or the file - "
        "then finish with `done` again: `status='done'` with a `proof` the system can check (a phrase "
        "visible on the page, a /work path, or a file you published), or `status='partial'` and what "
        "did not take effect. A phrase is checked word for word against the page `url` names, as it "
        "is now: take a snapshot of that page and copy a few words from it exactly - a paraphrase, "
        "or a figure you worked out, will not match."
    )


# Said by the system, not the model: a guest cart is not the person's cart, whatever the summary
# calls it. Keyed on the Outcome's typed `session`, never on the summary's words.
GUEST_NOTE: Final = (
    "*Done as a guest - nobody signed in, so this lives in this task's browser session, not in your account.*"
)


def outcome_narration(outcome: Outcome) -> str:
    """The thread's answer for a step that ended with an Outcome: a partial one says so first, and
    a change made without signing in says it was a guest's."""
    said = f"**Partly done.** {outcome.summary}" if outcome.status == "partial" else outcome.summary
    return f"{said}\n\n{GUEST_NOTE}" if outcome.session == "guest" else said


# The typed endings that stop a step at once: the next attempt would meet the same wall or verdict.
STOPS: Final = frozenset({"blocked", "failed"})


def stop_gotcha(outcome: Outcome) -> str:
    """A stopping ending's line in the step's evidence."""
    if outcome.status == "failed":
        return f"- **could not be done.** {outcome.summary}"
    return f"- **blocked ({BLOCKED_WHY[outcome.blocked_by]}).** {outcome.summary}"


def stop_reason(agent: str, outcome: Outcome) -> str:
    """Why the step stopped, as the task's failure reads it: the agent's own words."""
    if outcome.status == "failed":
        return f"{agent} could not do this: {outcome.summary[:300]}"
    return f"{agent} was blocked ({BLOCKED_WHY[outcome.blocked_by]}): {outcome.summary[:300]}"


BLOCKED_NEXT: Final[dict[str | None, str]] = {
    "login": "Take control of the browser and sign in, then ask it to try again.",
    "approval": "Approve the action (or do it yourself), then ask it to try again.",
    "person": "Take control of the browser and do the part only a person can, then ask it to try again.",
    "budget": "Raise the task's budget, then ask it to try again.",
    None: "Clear what stopped it, then ask it to try again.",
}


def blocked_diagnosis(outcome: Outcome, *, attempts: int, evidence: list[str]) -> Diagnosis:
    """A blocked step's escalation, in the words of the wall it met: not "could not
    produce a deliverable" - it stopped on purpose, and the person reading needs what to do.
    The headline is the card's title, so it names the wall and never the agent's id ("computer
    was blocked: approval needed" read as the person's own computer); it matches the step row's
    own label on the web ("Blocked: approval needed")."""
    wall = outcome.blocked_by
    return Diagnosis(
        what_failed=f"Blocked: {BLOCKED_WHY[wall]}" if wall is not None else "Blocked",
        why=outcome.summary,
        attempts=attempts,
        evidence=evidence,
        suggested_next=[BLOCKED_NEXT[outcome.blocked_by]],
    )


# Never "run it again": the same conversation reaches the same verdict.
FAILED_NEXT: Final = "Give it what it said was missing, or ask for it another way, then ask it to try again."


def failed_diagnosis(outcome: Outcome, *, attempts: int, evidence: list[str]) -> Diagnosis:
    """A `failed` step's escalation: the agent's own account of why it could not be done (the
    headline, like a blocked one's, never names the agent's id)."""
    return Diagnosis(
        what_failed="Could not be done",
        why=outcome.summary,
        attempts=attempts,
        evidence=evidence,
        suggested_next=[FAILED_NEXT],
    )


# ── the budget brake ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Notice:
    lead: str  # the stable words a conversation is searched for: a resumed run is never told twice
    text: str


# A notice orders the work; it never ends it. "Finish the essential part now, then finish with
# `done`" made the computer stop at 75% with nothing read), so the cap's
# gate opened after the step had already quit and a raise resumed nothing. At the cap the guard
# pauses the run and the person decides.
_KEEP_GOING: Final = (
    "keep working - at the cap the task pauses and asks the person whether to raise it, so never "
    "stop early or cut the work short because of the budget"
)
# A run that cannot park (it may not ask) has no gate: at the cap it is told to finish (BUDGET_FINISH).
_KEEP_GOING_UNPARKED: Final = (
    "keep working - at the cap you will be told to stop and finish with what you have, so never "
    "stop early, and get the essential part done while there is budget"
)
_MARKS: Final = (
    (
        Decimal("0.75"),
        "You have used three quarters of this task's budget",
        "stay on the essential part and avoid detours",
    ),
    (
        Decimal("0.5"),
        "You have used half of this task's budget",
        "do the essential part first",
    ),
)


def budget_notice(figures: BudgetFigures, *, can_park: bool = True) -> Notice | None:
    """The highest mark crossed below the cap - at 50% and at 75%, once each. At the cap the guard
    parks instead of talking - or, for a run that cannot park (`can_park=False`), holds its calls;
    each is promised only what will happen to it."""
    if figures.ceiling_usd <= 0:
        return None
    share = figures.spent_usd / figures.ceiling_usd
    keep_going = _KEEP_GOING if can_park else _KEEP_GOING_UNPARKED
    for mark, lead, advice in _MARKS:
        if mark <= share < 1:
            spent = f"{said_usd(figures.spent_usd)} of {said_usd(figures.ceiling_usd)}"
            return Notice(lead, f"{lead} ({spent}); {advice}; {keep_going}.")
    return None


# The metadata on the answer of a call that never ran (held by the guard, or not run past the budget):
# a recall of the history skips it, whatever text the answer starts with.
NOT_RUN_METADATA: Final[dict[str, str]] = {"guard": "not_run"}

# True whether or not the cap moved: a task-scope accept may resume the run without a raise.
BUDGET_RESUMED: Final = (
    "Not run: the task reached its budget before this ran. The run may go on now - spend carefully, "
    "the budget may still be at its cap. Re-issue it if it still fits."
)
# A question that shared the held turn parks as itself, so the budget did not hold it - it just
# never reached a person (the budget's gate took the turn).
BUDGET_ASK_AGAIN: Final = (
    "Not asked: the task reached its budget in the same turn, so this question never reached a "
    "person. The run may go on now - ask again if you still need the answer."
)
# A run that cannot park (it may not ask) has no gate to pause on at the cap: every call is held
# with this, and the run ends with what it has. Further work is the Bursar's to buy.
BUDGET_FINISH: Final = (
    "Not run: the task reached its budget, and this step cannot pause to ask for more. Stop now and "
    "finish with what you have: publish what you built, if you publish your work, and say what is "
    "left undone."
)
# The same hold when the workspace spent its allowance (ADR-103): the run is not blamed on a budget
# the task may not even have.
ALLOWANCE_FINISH: Final = (
    "Not run: this workspace has reached its spending allowance, and this step cannot pause until it "
    "is back. "
    "Stop now and finish with what you have: publish what you built, if you publish your work, and "
    "say what is left undone."
)
BUDGET_CONTINUE: Final = (
    "The run may go on past where the task's budget stopped it - spend carefully, the budget may "
    "still be at its cap. Continue from where you stopped; do not redo what already took effect."
)
# Why a task stopped at its budget gate, on the cancel's frame: the person's no to more spend.
BUDGET_STOP: Final = "stopped at the task's budget"

BUDGET_DECLINED: Final = (
    "Stopped at the task's budget: the cap was kept, so the work stopped where it was. What was "
    "done so far stays on the Machine."
)


# ── the stall brake ───────────────────────────────────────────────────────────────────────────


def stall_notice(tool: str, *, outcome: bool = True) -> str:
    """The first stall's word. `outcome`: the agent ends with `done` (the computer); any other
    guarded agent ends with its answer, and has no `done` to call."""
    finish = (
        "finish with `done(status='partial')` and say what blocked you"
        if outcome
        else "finish now with what you have and say what blocked you"
    )
    return f"You are repeating `{tool}` without effect. Change approach, or {finish}."


FINISH_NOW: Final = (
    "Stop now: you have repeated the same thing without effect twice. Finish with "
    "`done(status='partial')`: say what you did and what blocked you."
)
# The same stop for a guarded agent that ends with its answer (a document, a program, a reply).
FINISH_NOW_ANSWER: Final = (
    "Stop now: you have repeated the same thing without effect twice. Finish with what you have: "
    "publish what you built, if you publish your work, and say what you did and what blocked you."
)
# The last request a step's ceiling allows: the tools are off, so this turn is the answer.
FORCED_ANSWER: Final = (
    "This is your last turn: the tools are closed. Answer now from what you have already read, citing "
    "it as before. Say how confident you are, and name what you could not find or verify."
)
STALL_HELD: Final = "held: the run was told to finish"  # the journal's reason for a call the guard held

# ── the browser's loop words: soft, once a run, never a brake ────────────────────────────────

# The page came back the same several times in a row (looks, scrolls, acts that changed nothing):
# said once, before the stall brake has any reason to speak.
PAGE_UNCHANGED: Final = (
    "This page has come back unchanged several times: looking again or scrolling will not show you "
    "more. Use the site's own search box, its sort and its filters, or `browser_snapshot(find=...)` "
    "for one thing on a long page."
)
# One field entry per response on the same page, twice running (Anthropic's batch reminder).
BATCH_REMINDER: Final = (
    "You are making one browser call per turn on the same page. When you can predict the next steps "
    "- every field of a form and its submit, a search box and Enter, a filter and the sort - make "
    "those calls together in ONE response, the call that changes the page last."
)
