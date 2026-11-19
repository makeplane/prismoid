# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Conversation shapes (ADR-057) - talk, as values. A Conversation executes nothing.

The constitution's third twin: Capture runs nothing, a Routine executes nothing, and **a
Conversation executes nothing - every piece of work it needs mints an ordinary Task.** The
values here are the verbs a message resolves to, the FACTS the door reads (never the words'
meaning), the verdict it returns, and the proposal a bot may make. Nothing here can start spend:
consent lives in the verdict, and the verdict is a pure function of facts (`operations/chat_verdict.py`).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from prismoid.schemas.ids import ConversationId


class MessageRole(StrEnum):
    USER = "user"
    BOT = "bot"
    SYSTEM = "system"  # the machine's own lines: started, delivered, waiting, refused


class Verdict(StrEnum):
    """What a USER message resolved to. One verb per message, journaled."""

    ANSWER = "answer"  # one streaming model call, text only
    PROPOSE = "propose"  # answered AND attached a task proposal card
    START = "start"  # an ordinary Task was minted
    STEER = "steer"  # the message steered a running chat-born task
    FOLLOW_UP = "follow_up"  # the message continued a settled chat-born task
    REFUSED = "refused"  # the door said no (caps, disabled) - a row wearing its reason
    NONE = "none"  # bot and system rows carry no verdict


class Consent(StrEnum):
    """Where a START's permission came from - the only three sources there are."""

    GESTURE = "gesture"  # the explicit Start-as-task gesture
    GRANT = "grant"  # the bot's standing work_mode=auto, within its cap, from its owner
    PROPOSAL = "proposal"  # a tap on a proposal card, or a yes the bot heard on it (`accept_pending`)
    NONE = "none"


class WorkMode(StrEnum):
    """The bot's standing dial: may it start work by itself?"""

    ASK = "ask"  # it proposes, you tap
    AUTO = "auto"  # default: it starts on its own, from YOUR messages, inside its per-task cap
    NEVER = "never"  # a pure talker


class ThreadsMode(StrEnum):
    """The bot's threads dial: one chat, or a chat per topic."""

    ONE = "one"  # the bot IS its one conversation: no list, no New chat
    MANY = "many"  # default: many cheap chats; the list appears once a second exists


class ConversationSource(StrEnum):
    WEB = "web"
    CHANNEL = "channel"  # email / webhook / telegram - the transport lives on the channel row


# The system states its own fact (the DELIVERY_LAW / PAYLOAD_LAW pattern): appended to a bot's
# instructions ONLY on the conversation lane. The bot never gets a frame here, so nothing it
# says can become an artifact by accident; work is a proposal, and proposals wait for consent.
CHAT_LAW = (
    "\n\n[You are in a CONVERSATION, not a task. Answer like a skilled colleague: directly, in "
    "one message, plainly, and COMPLETELY - a memo, a summary, a list, a table, a draft email, "
    "a code snippet, a plan all belong in the reply itself, in markdown, whole. Propose a task "
    "with `propose_task` ONLY when the result must be a FILE in a named format, a RUN (code "
    "executed, a sandbox, a browser), a VERIFIED deliverable with sources, or an action in a "
    "connected service - and then say what you proposed. Never propose what the reply can "
    "simply be, never claim work you did not start, and when something is ambiguous ask in "
    "your reply like a person would; there are no forms here.]"
)

MAX_MESSAGE_CHARS: Final = 20_000
MAX_HISTORY_TURNS: Final = 30  # user+bot rows inlined into a reply, newest-first clipped
MAX_HISTORY_CHARS: Final = 24_000  # ~6k tokens - the express thread budget's neighbour
MAX_PROPOSAL_GOAL_CHARS: Final = 4_000
DEFAULT_TASK_BUDGET_USD: Final = 2.0  # the routines default, reused


class ConversationFacts(BaseModel):
    """Everything the door may see for one message - values assembled by operations, no I/O."""

    model_config = ConfigDict(frozen=True)

    gesture: bool = False  # the explicit Start-as-task gesture
    attachments: int = 0
    sender_is_owner: bool = True
    work_grant: bool = False  # a channel's standing grant for non-owner senders
    work_mode: str = WorkMode.ASK.value
    pending_proposal: bool = False  # a proposal card is waiting in this conversation
    task_anchor: str | None = None  # the anchored task's state: running | settled | None
    # How the anchor was set: "reply" - the card's explicit Reply affordance; "card" - the bot
    # handed the line to the card right above it (`continue_card`). Empty when unanchored.
    anchored_by: str = ""
    too_long_to_steer: bool = False  # longer than a steer may be (`steering.DIRECTIVE_MAX`)


class VerdictDecision(BaseModel):
    """The door's answer for one message, BEFORE the model runs; `settle_verdict` finishes the
    record after the model spoke."""

    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    reason: str
    consent: Consent = Consent.NONE


# ── Lane 2 for a conversation: the reply, as it is generated ─────────────────────────────────


class _ChatLive(BaseModel):
    """An ephemeral frame of one conversation's reply - the task lane's Lane-2 law, one socket
    over: no seq, therefore no SSE `id:` (a delta must never advance `Last-Event-ID`), anchored
    on `after_seq` (the last durable MESSAGE seq) so a client can order it against the record.
    Every stream ends in a durable bot row: a frame lost in a disconnect never mattered."""

    model_config = ConfigDict(frozen=True)

    lane: Literal["live"] = "live"
    conversation_id: ConversationId
    after_seq: int = 0
    at: dt.datetime


class ChatStart(_ChatLive):
    type: Literal["chat.start"] = "chat.start"
    bot: str = ""


class ChatDelta(_ChatLive):
    """Markdown, append-only - never a retro-edit of what already streamed."""

    type: Literal["chat.delta"] = "chat.delta"
    text: str


class ChatActivity(_ChatLive):
    """A tool the reply is using, as it happens - the web's quiet "searching…" line. Labels
    follow the journal's redaction posture (locators, never content)."""

    type: Literal["chat.activity"] = "chat.activity"
    phase: Literal["start", "end"]
    tool: str = ""
    tool_call_id: str = ""  # pairs an `end` with its `start` - tools run in parallel
    label: str = ""
    ok: bool | None = None
    duration_ms: int = 0


class ChatEnd(_ChatLive):
    """Terminates in the durable bot row carrying the same markdown in full."""

    type: Literal["chat.end"] = "chat.end"


type ChatLiveEvent = Annotated[ChatStart | ChatDelta | ChatActivity | ChatEnd, Field(discriminator="type")]
ChatLiveAdapter: TypeAdapter[ChatLiveEvent] = TypeAdapter(ChatLiveEvent)
CHAT_LIVE_CLASSES: Final = (ChatStart, ChatDelta, ChatActivity, ChatEnd)


# A document's file format, as the task door takes it (`deliverable_format`).
type DocumentFormat = Literal["md", "docx", "pdf", "html"]


class TaskProposal(BaseModel):
    """What a bot may put on the table: a goal in its own words, the deliverable
    it expects, why it needs a run. Lives on the bot's message row; `status` is the card."""

    model_config = ConfigDict(frozen=True)

    goal: str = Field(min_length=1, max_length=MAX_PROPOSAL_GOAL_CHARS)
    deliverable: str | None = None  # a kind word: document | spreadsheet | deck | code ...
    why: str = Field(default="", max_length=300)
    direct: bool = True  # the bot does it (default); False hands it to the planner's seats
    status: str = "pending"  # pending | started | declined
    task_id: str | None = None
    format: DocumentFormat | None = None  # a document's file format; becomes the task's deliverable_format

    @model_validator(mode="before")
    @classmethod
    def _format_is_a_document_choice(cls, data: Any) -> Any:
        """A format alone implies a document (the task door's rule); beside another kind it is dropped,
        so a grant-start never 422s on it."""
        if not isinstance(data, dict):
            return data
        fields = cast("dict[str, Any]", data)
        if not fields.get("format"):
            return fields
        kind = fields.get("deliverable") or "document"
        return {**fields, "deliverable": kind, "format": fields["format"] if kind == "document" else None}
