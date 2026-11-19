# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The lanes around a turn, declared as data: a follow-up the Lead replies to, a bot's direct
line, the receipt that follows an answer, a sweep that fans out, and a fleet. See `all.py` for how a scenario is added and
`harness/moves_lanes.py` for what a move does."""

from __future__ import annotations

from decimal import Decimal

from prismoid.schemas.longform import OutlineResult, SectionOutline
from prismoid.schemas.outputs import SubmittedPart
from prismoid.schemas.verification import JudgeVerdict, Verdict
from tests.contract.harness.moves_lanes import fleet_of, follow_up, jobs, judge_seat, published_bot, receipts
from tests.contract.harness.rig import Scenario
from tests.contract.harness.voices import leads, publishes, says, step, submits, writes
from tests.contract.harness.world import World

# A judge's opinion counts only when its call was metered (the ledger names the model that gave it).
JUDGE_IS_METERED = World(price=Decimal("0.0001"), billed=("judge",))
JUDGED = submits(JudgeVerdict(verdict=Verdict.PASS, rationale="It answers the ask."))
REJECTED = submits(JudgeVerdict(verdict=Verdict.FAIL, rationale="It does not answer the ask."))

FOLLOW_UP = [
    # The second message is a question about the work: the Lead replies on the same task.
    Scenario(
        "follow_up_reply",
        "Research the Eddington correction",
        {
            "lead": (leads(step("s1", "researcher")), says("Three sources.")),
            "researcher": (says("The findings, cited."),),
        },
        then=(follow_up("Which sources did you use?"), jobs("run_task")),
    ),
]

DIRECT_BOT = [
    # A person talks to one bot directly: the plan is the bot's one step, made in code, and the
    # Lead is never asked; memory_extract reads the turn after delivery.
    Scenario(
        "direct_bot",
        "Recap this meeting",
        {"scribe": publishes("recap.md")},
        dials={"bots": ["scribe"], "direct": True},
        given=(published_bot("scribe"),),
    ),
]

VERIFY = [
    # The answer comes first: the step is done and the task delivered with no verdict. The queue was
    # handed the receipt's job meanwhile; when it runs (the real document suite, then the judge), the
    # signed verdict lands after the delivery.
    Scenario(
        "publish_then_verify",
        "Research the Eddington correction",
        {
            "lead": (leads(step("s1", "researcher")),),
            "researcher": (says("The findings, cited."),),
            "judge": (JUDGED,),
        },
        given=(judge_seat,),
        world=JUDGE_IS_METERED,
        then=(jobs("verify_artifact"), receipts()),
    ),
    # The judge rejects it; the task stays delivered.
    Scenario(
        "publish_then_verify_fails",
        "Research the Eddington correction",
        {
            "lead": (leads(step("s1", "researcher")),),
            "researcher": (says("The findings, cited."),),
            "judge": (REJECTED,),
        },
        given=(judge_seat,),
        world=JUDGE_IS_METERED,
        then=(jobs("verify_artifact"), receipts()),
    ),
]

STARTUPS = "List 60 US climate startups with their funding"
SCATTER = [
    # "Find N things" at sweep scale is the Lead's turn: it plans the research.
    Scenario(
        "scatter",
        STARTUPS,
        {"lead": (leads(step("s1", "researcher")),), "researcher": (says("The startups, cited."),)},
    ),
    # Five things is no sweep: the answer is a reply.
    Scenario(
        "no_scatter_below_the_sweep",
        "List 5 good CRMs",
        {"lead": (says("HubSpot, Salesforce, Pipedrive, Zoho and Close."),)},
    ),
]

GUIDE = "Write a 30-page guide to home composting"
CHAPTERS = ("Why compost", "Building the bin", "Keeping it healthy")
OUTLINE = submits(
    OutlineResult(
        filename="home-composting",
        title="Home Composting",
        continuity_bible="Second person; the container is always 'the bin'.",
        summary="A guide to composting at home.",
        sections=[
            SectionOutline(id=f"s0{i}", title=title, brief=f"Cover {title.lower()}.", target_words=3000)
            for i, title in enumerate(CHAPTERS, start=1)
        ],
    )
)

LONGFORM = [
    # Past one run's words: the writer designs the outline, the outline expands into its parts in order,
    # each part hands its summary to the next, and the maker assembles the parts into the document. The
    # document is delivered first; its receipt is signed after, like any deliverable's.
    # The page dial is the recipe: code plans it with no Lead run.
    Scenario(
        "longform",
        GUIDE,
        {
            "writer": (
                OUTLINE,
                *(
                    submits(SubmittedPart(content=f"## {t}\n\nText.", summary=f"{t} covered."))
                    for t in CHAPTERS
                ),
            ),
            "maker": publishes("home-composting.md"),
            "judge": (JUDGED,),
        },
        {"target_pages": 30},
        given=(judge_seat,),
        world=JUDGE_IS_METERED,
        then=(jobs("verify_artifact"), receipts()),
    ),
]

PRICES = "Should we raise prices 10%?"
ANSWER = writes("Yes: margins are thin and churn is low.", title="Raise prices")
TABLE = writes(
    "| Model | Position (one line) | Strongest point | Caveat it flags |\n"
    "|---|---|---|---|\n"
    "| first | Raise them | Thin margins | Churn may rise |\n"
    "| second | Raise them | Low churn | Margins may recover |\n\n"
    "## Where they actually agree\n\nBoth would raise prices.\n\n"
    "## Where they differ\n\nNo disagreements.",
    title="Raise prices 10%",
)
FLEET_VOICES = {"writer": (ANSWER, ANSWER, TABLE), "judge": (JUDGED, JUDGED, JUDGED)}

FLEET = [
    # Every seat answers the same ask in one wave, each reviewed in line; then the writer lays the answers
    # in one table, reviewed in line too, and the table is what the task delivers.
    Scenario(
        "fleet",
        PRICES,
        FLEET_VOICES,
        {"mode": "fleet"},
        given=(judge_seat, fleet_of(2)),
        world=JUDGE_IS_METERED,
    ),
    # A budget buys the seats whose runs fit it beside the table: the dear seat never sits.
    Scenario(
        "fleet_within_budget",
        PRICES,
        FLEET_VOICES,
        {"mode": "fleet", "budget": {"usd": "1.00"}},
        given=(judge_seat, fleet_of(2, dear=1)),
        world=JUDGE_IS_METERED,
    ),
]

LANES: list[Scenario] = [*FOLLOW_UP, *DIRECT_BOT, *VERIFY, *SCATTER, *LONGFORM, *FLEET]
