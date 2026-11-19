# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen's shapes: the control lease as the task page and the CLI
see it, and the screen socket's ticket. `*In` = request, `*Out` = response. The holder is a user -
`{id, name}` - never "actor"."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from prismoid.conf import settings
from prismoid.schemas.screen import CONN_PATTERN, LeaseView


class ScreenControlIn(BaseModel):
    """Take control. `conn` is the tab's connection id (`CONN_PATTERN`, as every conn validator),
    the one connection whose input will count; `private` turns the mask on for everyone else."""

    conn: str = Field(pattern=CONN_PATTERN)
    private: bool = False


class ScreenHolderOut(BaseModel):
    id: uuid.UUID
    name: str


class ScreenControlOut(BaseModel):
    holder: ScreenHolderOut
    private: bool
    since: dt.datetime
    until: dt.datetime
    cap_at: dt.datetime | None = None


def control_out(view: LeaseView) -> ScreenControlOut:
    return ScreenControlOut(
        holder=ScreenHolderOut(id=view.holder_id, name=view.holder_name),
        private=view.private,
        since=view.since,
        until=view.until,
        cap_at=view.cap_at,
    )


class ScreenTicketIn(BaseModel):
    """Who asks for a screen socket: the tab's connection id (`CONN_PATTERN`, as every conn
    validator) - the one the input gate compares with the lease's."""

    conn: str = Field(pattern=CONN_PATTERN)


class ScreenTicketOut(BaseModel):
    """A single-use `st_` ticket for `GET /v1/tasks/{id}/screen?ticket=…`, good for `expires_in`
    seconds. An admission, not a credential: signed, single-use, short-lived, never stored."""

    ticket: str
    expires_in: int


def ticket_out(ticket: str) -> ScreenTicketOut:
    return ScreenTicketOut(ticket=ticket, expires_in=settings.computer_use.ticket_ttl_s)
