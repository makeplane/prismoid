# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The gates of a turn, declared as data: a question asked mid-run and its answers, an approval, a
connect, the budget gate; and the parked turns kept as resume fixtures. See `all.py` for how a
scenario is added and `harness/answers.py` for how a gate is answered.

A resume fixture exists for each gate a step parks on: a question, an approval, a connect and a
pause. The budget gate has none: it parks the turn's own `run_task` between waves, and resuming
that needs the parent workflow replayed from the durable engine's journal, which this harness runs
inline and does not keep.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.connector import Connector
from prismoid.schemas.connectors import ConnectorStatus
from tests.contract.harness.answers import accepts, at_park, cancels_gate, declines, lapses
from tests.contract.harness.people import resumes
from tests.contract.harness.resume import Resume
from tests.contract.harness.rig import Scenario
from tests.contract.harness.voices import calls, leads, step, writes
from tests.contract.harness.world import World, mail_service
from tests.contract.scenarios.steering import PAUSES

ASK = "Write me a memo on the Eddington correction"
WHICH = calls("ask_user", questions=[{"question": "Which audience?", "options": ["board", "press"]}])
MEMO = writes("The memo.")
ASKS = {"lead": (leads(step("s1", "writer")),), "writer": (WHICH, MEMO)}

QUESTIONS = [
    # The writer stops to ask: its step parks, the task waits, the answer wakes the same run.
    Scenario("ask_accepted", ASK, ASKS, stage=at_park(accepts(q1="board"))),
    # "Proceed without me": the run goes on with no answer.
    Scenario("ask_declined", ASK, ASKS, stage=at_park(declines())),
    # Cancel from the gate's card stops the whole task.
    Scenario("ask_cancelled", ASK, ASKS | {"writer": (WHICH,)}, stage=at_park(cancels_gate())),
]

# A two-step chain under a cap its first step spends: the second wave parks on the budget gate.
CAPPED = {"usd": "0.01"}
CHAIN = {
    "lead": (leads(step("s1", "writer"), step("s2", "writer", "s1")),),
    "writer": (writes("A draft."), MEMO),
}
SPENDS = World(price=Decimal("0.01"), billed=("writer",))

BUDGET = [
    # The owner accepts: the parked wave runs.
    Scenario("budget_accepted", ASK, CHAIN, {"budget": CAPPED}, stage=at_park(accepts()), world=SPENDS),
    # The owner keeps the cap: nothing more is bought and the task ends.
    Scenario(
        "budget_declined",
        ASK,
        CHAIN | {"writer": (writes("A draft."),)},
        {"budget": CAPPED},
        stage=at_park(declines()),
        world=SPENDS,
    ),
]


async def connected(session: AsyncSession, actor: Actor) -> None:
    """The owner has connected a mail service (its tools are probed live when a step mounts it)."""
    session.add(
        Connector(
            owner_actor_id=actor.id,
            name="Mail",
            slug="mail",
            server_url="https://mail.example.test/mcp",
            status=ConnectorStatus.LIVE,
        )
    )
    await session.flush()


async def asks_before_send(session: AsyncSession, actor: Actor) -> None:
    """The owner's connected mail service, with its `send` set to ask (one PATCH on her own row)."""
    await connected(session, actor)
    row = (await session.scalars(select(Connector).where(Connector.slug == "mail"))).one()
    row.tool_policies = {"send": "ask"}
    await session.flush()


SEND = calls("mail_send", to="ada@example.test", body="the memo")
SENDS = {"lead": (leads(step("s1", "writer")),), "writer": (SEND, MEMO)}
MAIL = World(service=mail_service())

APPROVALS = [
    # The writer calls a tool its owner set to ask: nothing runs, the step parks on an approval.
    # Approving lets the same call run.
    Scenario(
        "approval_approved",
        ASK,
        SENDS,
        {"connectors": ("mail",)},
        given=(asks_before_send,),
        stage=at_park(accepts()),
        world=MAIL,
    ),
    # Denying tells the model it was refused; the run goes on without it.
    Scenario(
        "approval_denied",
        ASK,
        SENDS,
        {"connectors": ("mail",)},
        given=(asks_before_send,),
        stage=at_park(declines()),
        world=MAIL,
    ),
    # Nobody answers for a day: silence is a no.
    Scenario(
        "approval_lapsed",
        ASK,
        SENDS,
        {"connectors": ("mail",)},
        given=(asks_before_send,),
        stage=at_park(lapses),
        world=MAIL,
    ),
]

WANT_MAIL = calls("request_connector", service="mail", why="to look something up")
WANTS_MAIL = {"lead": (leads(step("s1", "writer")),), "writer": (WANT_MAIL, MEMO)}
# Once connected, the service's tools are the step's: the read it then makes is the proof.
USES_MAIL = {
    "lead": (leads(step("s1", "writer")),),
    "writer": (WANT_MAIL, calls("mail_search", query="the memo"), MEMO),
}

CONNECTS = [
    # The writer needs a service it does not hold: its step parks on a connect gate. Accepting attaches
    # the owner's own connector to the task; its first read then runs.
    Scenario(
        "connect_accepted",
        ASK,
        USES_MAIL,
        given=(connected,),
        stage=at_park(accepts()),
        world=MAIL,
    ),
    # Skipping: the run goes on without the service.
    Scenario("connect_declined", ASK, WANTS_MAIL, given=(connected,), stage=at_park(declines()), world=MAIL),
    Scenario(
        "connect_cancelled",
        ASK,
        WANTS_MAIL | {"writer": (WANT_MAIL,)},
        given=(connected,),
        stage=at_park(cancels_gate()),
        world=MAIL,
    ),
]

GATES: list[Scenario] = [*QUESTIONS, *APPROVALS, *CONNECTS, *BUDGET]


def parked_as(name: str) -> Scenario:
    """The scenario of that name, whose first park a resume fixture keeps."""
    return next(s for s in [*QUESTIONS, *APPROVALS, *CONNECTS, *PAUSES] if s.name == name)


# A parked turn whose rows are kept (`fixtures/`) and resumed: the park, saved by the engine of the
# day, must come back as it did. The models' replies are what they say after the park.
WRITES = {"writer": (MEMO,)}
RESUMES = [
    Resume("resume_ask", parked_as("ask_accepted"), WRITES, (accepts(q1="board"),)),
    Resume("resume_approval", parked_as("approval_approved"), WRITES, (accepts(),)),
    Resume(
        "resume_connect",
        parked_as("connect_accepted"),
        {"writer": (calls("mail_search", query="the memo"), MEMO)},
        (accepts(),),
    ),
    Resume("resume_pause", parked_as("pause_parks_the_step"), WRITES, (resumes(),)),
]
