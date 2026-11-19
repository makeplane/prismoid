# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The stream ticket's shape. `*Out` = response."""

from __future__ import annotations

from pydantic import BaseModel

from prismoid.schemas.stream_tickets import STREAM_TICKET_TTL_S


class StreamTicketOut(BaseModel):
    """A single-use `et_` ticket that opens one event stream (`GET …/events?ticket=…`) within
    `expires_in` seconds. An admission, not a credential: the stream it opens re-proves the
    credential that minted it, and it opens nothing else."""

    ticket: str
    expires_in: int


def stream_ticket_out(ticket: str) -> StreamTicketOut:
    return StreamTicketOut(ticket=ticket, expires_in=STREAM_TICKET_TTL_S)
