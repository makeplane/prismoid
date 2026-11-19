# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A connector approval's arguments are the creator's to read; a teammate reads what waits on them.

The arguments run on the creator's credential (an email's recipients and body), so where a gate
leaves the server - the gates API and the task stream - a project member other than the creator
sees each held call's connector and tool, never its arguments. A browser approval and a budget gate
read the same for everyone; a device-away gate's command stays the creator's, as before.
"""

from __future__ import annotations

import datetime as dt
import uuid

from prismoid.schemas.events import GateOpened, redact_for
from prismoid.schemas.gate_view import viewer_reason
from prismoid.schemas.ids import GateId, TaskId
from prismoid.schemas.plan import GateKind

APPROVAL = (
    "Gmail: send_email\nbody: the quarterly numbers\nto: cfo@example.com\n\nGmail: search\nquery: invoices"
)


def test_a_teammate_reads_which_calls_wait_and_never_their_arguments() -> None:
    assert viewer_reason(APPROVAL, GateKind.APPROVE, "connector") == "Gmail: send_email\n\nGmail: search"


def test_a_browser_approval_reads_the_same_for_everyone() -> None:
    reason = "Allow the browser to open example.com?\nthe page asked for it"
    assert viewer_reason(reason, GateKind.APPROVE, "browser") == reason


def test_the_task_stream_applies_the_cut_for_a_member_who_is_not_the_creator() -> None:
    creator, member = uuid.uuid4(), uuid.uuid4()
    opened = GateOpened(
        seq=7,
        task_id=TaskId(uuid.uuid4()),
        at=dt.datetime(2026, 11, 12, tzinfo=dt.UTC),
        gate_id=GateId(uuid.uuid4()),
        kind=GateKind.APPROVE,
        title="Allow 2 actions?",
        reason=APPROVAL,
        source="connector",
    )
    assert (
        redact_for(opened, viewer_id=member, creator_id=creator).reason
        == "Gmail: send_email\n\nGmail: search"
    )
    assert redact_for(opened, viewer_id=creator, creator_id=creator).reason == APPROVAL
