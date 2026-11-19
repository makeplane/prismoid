# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# backend/tests/unit/operations/execution/test_engine_browser_wiring.py
"""The engine's browser wiring: the frame profile follows the agent,
the kill switch stops dispatch with a reason, the allowlist is composed from four homes, an
approved browser call's resume replays its ORIGINAL args plus the fingerprint it was approved
with (never a bare `ToolApproved()`), and the recording is published as evidence, never the
deliverable a contract asked about."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai import DeferredToolRequests, ToolApproved, ToolDenied
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.conf import ComputerUseSettings, settings
from prismoid.db.models.task import Task
from prismoid.operations import execution
from prismoid.operations.execution import answers as ex_answers
from prismoid.operations.execution import deferrals as ex_deferrals
from prismoid.operations.execution import giveback as ex_giveback
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.execution import ports as ex_ports
from prismoid.schemas.computer_use import DEFAULT_ALLOWLIST
from prismoid.schemas.contract import Contract
from prismoid.schemas.park import ParkKind, park_of
from tests.support.rigs.browser_wiring_rig import FakeFrame, facts, wiring_copy_dest


class _Session:  # the two reads the composer makes are patched at module level below
    pass


async def test_the_allowlist_is_the_union_of_four_homes(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.operations import egress_allowances as allow_ops

    async def grants(session: Any, task: Any) -> list[str]:
        return ["sso.vendor.com"]

    async def owner_hosts(session: Any, actor_id: Any) -> list[str]:
        return ["always.example.com"]

    monkeypatch.setattr(allow_ops, "grants_for_task", grants)
    monkeypatch.setattr(allow_ops, "hosts_for_owner", owner_hosts)

    class _Task:
        creator_actor_id = "actor"

    allowed = await execution._browser_allowlist(  # pyright: ignore[reportPrivateUsage]
        cast(AsyncSession, _Session()),
        cast(Task, _Task()),
        Contract(goal="g", allow_domains=["admin.example.com"]),
    )
    assert allowed == {"admin.example.com", "sso.vendor.com", "always.example.com", *DEFAULT_ALLOWLIST}


def test_disabled_computer_use_denies_a_browser_step_with_the_switch_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prismoid.errors import PolicyDenied

    monkeypatch.setattr(settings.computer_use, "enabled", False)
    with pytest.raises(PolicyDenied, match="PRISMOID_COMPUTER_USE_ENABLED"):
        execution._check_computer_use("browser")  # pyright: ignore[reportPrivateUsage]
    execution._check_computer_use("base")  # pyright: ignore[reportPrivateUsage]  # never gates a plain step
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    execution._check_computer_use("browser")  # pyright: ignore[reportPrivateUsage]


_COMPUTER_USE_ENV = (
    "PRISMOID_COMPUTER_USE_ENABLED",
    "PRISMOID_COMPUTER_USE_COMMAND_TIMEOUT_S",
    "PRISMOID_COMPUTER_USE_SNAPSHOT_WINDOW",
)


def test_settings_group_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Hermetic: the shipped defaults, read where no `.env` exists and the process env says
    # nothing - a developer's own `backend/.env` turning the switch on must not decide them.
    for name in _COMPUTER_USE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    defaults = ComputerUseSettings()
    assert defaults.enabled is False
    assert defaults.command_timeout_s == 30.0
    assert defaults.snapshot_window == 2


def test_settings_group_reads_the_env_file_like_every_other_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in _COMPUTER_USE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "PRISMOID_COMPUTER_USE_ENABLED=true\nPRISMOID_COMPUTER_USE_SNAPSHOT_WINDOW=3\n"
    )
    configured = ComputerUseSettings()
    assert configured.enabled is True
    assert configured.snapshot_window == 3


_FINGERPRINT = {
    "role": "button",
    "name": "Delete Grace Hopper",
    "host": "127.0.0.1",
    "path": "/records.html",
}


def _approval(resume: dict[str, object]) -> execution.ApprovalDeferral:
    """The approval park the resume reads off the replayed turn: each call, its tool and fingerprint."""
    calls = cast(dict[str, dict[str, Any]], resume["approval_calls"])
    return execution.ApprovalDeferral(
        items=[(c, str(m.get("connector", "")), str(m.get("tool", "")), "") for c, m in calls.items()],
        fingerprints={c: m.get("fingerprint") for c, m in calls.items()},
    )


def test_resume_rebuilds_a_browser_approval_with_its_original_args_and_fingerprint() -> None:
    """The toolset refuses an acting call (`browser.click/type/fill/select/press`) unless
    `expect` is the exact fingerprint it was approved with (`browser.py`'s module docstring).
    The resume must replay the ORIGINAL args the call ran with - read back out of the persisted
    history, never re-derived - plus that fingerprint, never a bare `ToolApproved()`.
    The gate names the DOTTED form (`browser.click`) - the match against
    `ToolCallPart.tool_name` (the model-facing, underscore form) converts."""
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="browser_click", tool_call_id="call-1", args={"ref": "e3.2", "confirm": False}
                )
            ]
        )
    ]
    resume: dict[str, object] = {
        "approval_action": "accept",
        "approval_calls": {
            "call-1": {"connector": "browser", "tool": "browser.click", "fingerprint": _FINGERPRINT},
        },
    }
    verdicts = ex_answers.verdicts(_approval(resume), ex_answers.v1_requests(history), resume)
    verdict = verdicts["call-1"]
    assert isinstance(verdict, ToolApproved)
    assert verdict.override_args == {"ref": "e3.2", "confirm": False, "expect": _FINGERPRINT}


def test_resume_matches_the_newest_call_when_a_tool_call_id_repeats() -> None:
    """Scanned NEWEST-first: a long resumed run's history can in principle carry an EARLIER
    call that reused the same id (a retried turn) - the pending call is always the run's LAST
    turn, and the newest match must win, never an accident of scan order."""
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="browser_click", tool_call_id="call-1", args={"ref": "stale"})]
        ),
        ModelResponse(
            parts=[ToolCallPart(tool_name="browser_click", tool_call_id="call-1", args={"ref": "fresh"})]
        ),
    ]
    resume: dict[str, object] = {
        "approval_action": "accept",
        "approval_calls": {"call-1": {"tool": "browser.click", "fingerprint": _FINGERPRINT}},
    }
    verdicts = ex_answers.verdicts(_approval(resume), ex_answers.v1_requests(history), resume)
    verdict = verdicts["call-1"]
    assert isinstance(verdict, ToolApproved)
    assert verdict.override_args == {"ref": "fresh", "expect": _FINGERPRINT}


def test_resume_denies_a_fingerprinted_call_whose_tool_name_does_not_match() -> None:
    """The id alone is not enough to trust a match - `_call_args` also requires the MODEL-
    facing tool name to agree, or a stale/forged id could pair a fingerprint meant for one
    call with a completely different call's args. Never falls back to an expect-only
    override, which is not a valid call and the toolset would refuse anyway for missing
    required args - denied outright instead."""
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="browser_type", tool_call_id="call-1", args={"ref": "e1"})]
        )
    ]
    resume: dict[str, object] = {
        "approval_action": "accept",
        "approval_calls": {"call-1": {"tool": "browser.click", "fingerprint": _FINGERPRINT}},
    }
    verdicts = ex_answers.verdicts(_approval(resume), ex_answers.v1_requests(history), resume)
    verdict = verdicts["call-1"]
    assert isinstance(verdict, ToolDenied)
    assert verdict.message == execution.APPROVAL_UNMATCHED


def test_resume_keeps_a_bare_approval_for_a_call_with_no_fingerprint() -> None:
    """A connector call, or `browser.open`'s allowlist ask, carries no fingerprint - `expect`
    means nothing to either, so the resume must not fabricate one."""
    resume: dict[str, object] = {
        "approval_action": "accept",
        "approval_calls": {
            "call-2": {"connector": "acme", "tool": "acme.send", "fingerprint": None},
            "call-3": {"connector": "browser", "tool": "browser.open", "fingerprint": None},
        },
    }
    verdicts = ex_answers.verdicts(_approval(resume), DeferredToolRequests(), resume)
    call2, call3 = verdicts["call-2"], verdicts["call-3"]
    assert isinstance(call2, ToolApproved) and isinstance(call3, ToolApproved)
    assert call2 == ToolApproved() and call3 == ToolApproved()
    assert call2.override_args is None


def test_resume_denies_every_pending_call_on_decline_or_overflow() -> None:
    resume: dict[str, object] = {
        "approval_action": "decline",
        "approval_calls": {
            "call-1": {"fingerprint": {"role": "button", "name": "x", "host": "h", "path": "/"}}
        },
    }
    verdicts = ex_answers.verdicts(_approval(resume), DeferredToolRequests(), resume)
    assert isinstance(verdicts["call-1"], ToolDenied)

    overflow: dict[str, object] = {**resume, "approval_action": "deny_overflow"}
    verdicts2 = ex_answers.verdicts(_approval(overflow), DeferredToolRequests(), overflow)
    denied = verdicts2["call-1"]
    assert isinstance(denied, ToolDenied)
    assert denied.message == execution.OUT_OF_APPROVALS


def test_resume_denies_a_fingerprinted_call_missing_from_history_never_an_expect_only_override() -> None:
    """A stale or corrupt park should not fail the WHOLE resume (it raises nothing), but it must
    not approve a call it cannot verify either - `ToolApproved(override_args={"expect": ...})`
    with no other args is not a real call, and the toolset would refuse it anyway for missing
    required args. Denied outright, with a reason the model can act on."""
    resume: dict[str, object] = {
        "approval_action": "accept",
        "approval_calls": {
            "ghost": {
                "tool": "browser.click",
                "fingerprint": {"role": "button", "name": "x", "host": "h", "path": "/"},
            }
        },
    }
    verdicts = ex_answers.verdicts(_approval(resume), DeferredToolRequests(), resume)
    ghost = verdicts["ghost"]
    assert isinstance(ghost, ToolDenied)
    assert ghost.message == execution.APPROVAL_UNMATCHED


async def test_publish_recording_copies_to_a_basename_that_never_collides_with_recording_dir() -> None:
    """CRITICAL 1 (review, round 2): both drivers' publish runner tars/copies TO
    `{ARTIFACTS}/{basename(path)}` - the exact path `frame.recording_dir` already occupies. A
    copy that merely changes the PARENT directory (the old `/work/recording-{attempt}`) still
    collides, because the target is keyed on the basename alone - `{ARTIFACTS}/recording-1` IS
    `frame.recording_dir`, and the runner's write hits the directory it is reading from
    (`IsADirectoryError`, silently swallowed as "not published"). The copy's basename must
    differ - a nonce, not just a different parent - and its exit code must be checked."""
    from prismoid.ai.frames.layout import ARTIFACTS
    from prismoid.schemas.outputs import ArtifactType

    frame = FakeFrame()
    log, fact = facts()
    published = await ex_harvest.publish_recording(frame, object(), success=True, own_output=False, fact=fact)
    copy_dest = wiring_copy_dest(frame)
    assert os.path.basename(copy_dest) != os.path.basename(frame.recording_dir)
    assert copy_dest.startswith("/work/")
    assert published == (f"{ARTIFACTS}/{os.path.basename(copy_dest)}", "deliverable")
    publish_call = next(c for c in frame.calls if c[0] == "publish")
    assert publish_call[1] == copy_dest
    assert publish_call[2] == ArtifactType.RECORDING.value
    assert log[-1] == ("end", "Recording published (3 action(s)) · deliverable")


async def test_publish_recording_checks_the_copys_exit_code() -> None:
    """A `cp` that fails partway (disk full, a permissions quirk) must not be handed to
    `publish` as if the copy had succeeded - CRITICAL 1's other half."""
    frame = FakeFrame(copy_exit_code=1)
    log, fact = facts()
    published = await ex_harvest.publish_recording(frame, object(), success=True, own_output=False, fact=fact)
    assert published is None
    assert not any(c[0] == "publish" for c in frame.calls)
    assert log[-1] == ("end", "Recording not published: copy failed (exit 1)")


async def test_publish_recording_is_working_role_when_the_step_published_its_own_output_or_failed() -> None:
    frame = FakeFrame()
    log, fact = facts()
    await ex_harvest.publish_recording(frame, object(), success=True, own_output=True, fact=fact)
    assert log[-1] == ("end", "Recording published (3 action(s)) · working")

    frame2 = FakeFrame()
    log2, fact2 = facts()
    await ex_harvest.publish_recording(frame2, object(), success=False, own_output=False, fact=fact2)
    assert log2[-1] == ("end", "Recording published (3 action(s)) · working")


async def test_publish_recording_never_raises_when_finalize_or_publish_fails() -> None:
    """Evidence is not authority: a finalize error or a publish refusal is a logged
    fact, never an exception the step's ladder has to catch."""
    frame = FakeFrame(finalize_ok=False)
    log, fact = facts()
    published = await ex_harvest.publish_recording(frame, object(), success=True, own_output=False, fact=fact)
    assert published is None
    assert log[-1] == ("end", "No recording (boom)")
    assert not any(c[0] == "publish" for c in frame.calls)

    frame2 = FakeFrame(fail_publish=True)
    log2, fact2 = facts()
    published2 = await ex_harvest.publish_recording(
        frame2, object(), success=True, own_output=False, fact=fact2
    )
    assert published2 is None
    assert log2[-1] == ("end", "Recording not published: RuntimeError")


async def test_publish_recording_removes_its_work_copy_once_published_or_refused() -> None:
    """The copy exists only to give `publish` a basename of its own. Left in `/work` it is the
    Machine's checkpoint and a fork's seed: screenshots of logged-in pages would ride into every
    sleep and every fork (which must never carry a login) and count toward the 2 GiB cap."""
    for fail_publish in (False, True):
        frame = FakeFrame(fail_publish=fail_publish)
        _log, fact = facts()
        await ex_harvest.publish_recording(frame, object(), success=True, own_output=False, fact=fact)
        copy_dest = wiring_copy_dest(frame)
        published_at = next(i for i, c in enumerate(frame.calls) if c[0] == "publish")
        assert ("exec", f"rm -rf {copy_dest}") in frame.calls[published_at + 1 :], (
            f"the copy outlived its publish (publish refused: {fail_publish})"
        )


async def test_a_recording_with_no_acts_is_working_evidence_never_the_deliverable() -> None:
    """IMPORTANT (whole-branch review): a navigator that opened nothing, clicked nothing (a
    screenshot is filed but is not an act) has no trail worth delivering."""
    frame = FakeFrame(actions=2, acting=0)
    log, fact = facts()
    published = await ex_harvest.publish_recording(frame, object(), success=True, own_output=False, fact=fact)
    assert published is not None and published[1] == "working"
    assert log[-1] == ("end", "Recording published (2 action(s)) · working")
    one = FakeFrame(actions=1, acting=1)
    _, fact = facts()
    published = await ex_harvest.publish_recording(one, object(), success=True, own_output=False, fact=fact)
    assert published is not None and published[1] == "deliverable"


async def test_a_recording_with_no_actions_is_never_published() -> None:
    """A computer step that only runs a shell command must not publish "Browser recording,
    attempt 1" - an empty page ("no actions were recorded") in the task's files. With
    no row at all there is nothing to show, so nothing is published; the copy is never made."""
    frame = FakeFrame(actions=0, acting=0)
    log, fact = facts()
    published = await ex_harvest.publish_recording(frame, object(), success=True, own_output=False, fact=fact)
    assert published is None
    assert not any(c[0] in ("publish", "exec") for c in frame.calls)
    assert log[-1] == ("end", "No recording (no browser actions)")


class _AllowFrame:
    """A frame that records its `browser` verbs and every `allow` list it is handed."""

    def __init__(self) -> None:
        self.verbs: list[dict[str, Any]] = []
        self.allowed: list[list[str]] = []

    async def browser(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.verbs.append(cmd)
        return {"ok": True, "held": True}

    async def allow(self, domains: list[str]) -> None:
        self.allowed.append(list(domains))


async def test_parking_a_browser_step_sends_hold_and_carries_only_its_once_widens() -> None:
    from prismoid.ai.tools.browser import BrowserPorts

    frame = _AllowFrame()
    ports = BrowserPorts(
        browse=frame.browser,
        allow=frame.allow,
        allowed={"admin.example.com", "always.example.com", "once.example.com", "127.0.0.1", "localhost"},
    )
    vouched = {"admin.example.com", "always.example.com"}  # what the allowlist's homes said
    deferrals: tuple[Any, ...] = (  # one of each park shape; `_browser_park` is generic over them
        execution.ApprovalDeferral(items=[]),
        execution.Deferral(tool_call_id="c", questions=[("q?", [])]),
        execution.ConnectDeferral(tool_call_id="c", service="s", why="w"),
        execution.PauseDeferral(calls=("c",)),
    )
    for deferral in deferrals:
        frame.verbs.clear()
        parked = await execution._browser_park(frame, ports, deferral, vouched=vouched)  # pyright: ignore[reportPrivateUsage]
        assert frame.verbs == [{"verb": "hold"}], "a parking browser step pins Chromium alive"
        # what the homes vouched for is recomputed on resume; loopback is toolset-local
        assert parked.browser_once == ("once.example.com",)
        assert park_of(ex_deferrals.park_record(parked, ParkKind.QUESTION, None)).browser_once == (
            "once.example.com",
        )

    plain = execution.ApprovalDeferral(items=[])
    frame.verbs.clear()
    assert await execution._browser_park(frame, None, plain, vouched=set()) is plain  # pyright: ignore[reportPrivateUsage]
    assert frame.verbs == [], "a non-browser step never talks to a sidecar"
    assert park_of(ex_deferrals.park_record(plain, ParkKind.APPROVAL, None)).browser_once == ()


async def test_an_always_grant_revoked_while_parked_is_gone_after_the_resume() -> None:
    """The park carries only what no home vouched for (a `once`); the resume recomputes the
    homes, so an always-grant revoked while the step sat parked is not handed back to the proxy
    by the list the step parked with."""
    from prismoid.ai.tools.browser import BrowserPorts

    frame = _AllowFrame()
    vouched = {"admin.example.com", "vendor.example.com"}  # vendor: an always-grant at park time
    ports = BrowserPorts(browse=frame.browser, allow=frame.allow, allowed={*vouched, "once.example.com"})
    parked = await execution._browser_park(  # pyright: ignore[reportPrivateUsage]
        frame, ports, execution.ApprovalDeferral(items=[]), vouched=vouched
    )
    resume_state = {**ex_deferrals.park_record(parked, ParkKind.APPROVAL, None)}
    homes_after_the_revoke = {"admin.example.com"}
    resumed = execution._with_parked_allowlist(homes_after_the_revoke, resume_state)  # pyright: ignore[reportPrivateUsage]
    assert resumed == {"admin.example.com", "once.example.com"}


async def test_a_once_grant_survives_a_second_park_in_the_same_attempt() -> None:
    """IMPORTANT (whole-branch review): accept `once` on a new host, then park again (a
    confirm ask) - the resume recomputes the allowlist from the DB (which never holds a
    `once`) and `frame.allow` REPLACES the proxy list. The park's live list must ride the
    resume, or the once host is silently revoked. Driven through the REAL toolset."""
    from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
    from pydantic_ai.messages import TextPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from prismoid.ai.tools.browser import BROWSER_TOOLS, BrowserPorts, browser_toolset

    frame = _AllowFrame()

    async def sidecar(cmd: dict[str, Any]) -> dict[str, Any]:
        frame.verbs.append(cmd)
        if cmd["verb"] == "inspect":
            return {"ok": True, "role": "link", "name": "More", "host": "once.example.com", "path": "/"}
        return {"ok": True, "url": "https://once.example.com/", "title": "Once"}

    db_allowed = {"admin.example.com"}  # what `_browser_allowlist` recomputes on every resume
    ports = BrowserPorts(browse=sidecar, allow=frame.allow, allowed=set(db_allowed))
    toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))
    assert toolset is not None

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not seen:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="browser_open", args={"url": "https://once.example.com/"})]
            )
        if len(seen) == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="browser_click", args={"ref": "e1.1", "confirm": True})]
            )
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(script), output_type=[str, DeferredToolRequests], toolsets=[toolset])
    first = await agent.run("go")
    assert isinstance(first.output, DeferredToolRequests)
    open_id = first.output.approvals[0].tool_call_id
    # accepted `once`: the toolset's own widen - no DB grant is written for a once
    second = await agent.run(
        message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={open_id: ToolApproved()}),
    )
    assert isinstance(second.output, DeferredToolRequests), "the confirm click parks again"
    assert "once.example.com" in frame.allowed[-1]

    parked = await execution._browser_park(  # pyright: ignore[reportPrivateUsage]
        frame,
        ports,
        ex_deferrals.approval_deferral(second.output),
        vouched=db_allowed,
    )
    resume_state = {**ex_deferrals.park_record(parked, ParkKind.APPROVAL, None)}

    # The resume: DB homes recomputed (no once host), unioned with the park's live list, and
    # handed to `frame.allow` - which REPLACES the proxy list.
    resumed = execution._with_parked_allowlist(db_allowed, resume_state)  # pyright: ignore[reportPrivateUsage]
    await frame.allow(sorted(resumed))
    assert "once.example.com" in frame.allowed[-1], "a once grant must survive the second park"
    assert "once.example.com" not in execution._with_parked_allowlist(db_allowed, {})  # pyright: ignore[reportPrivateUsage]


def test_the_computer_reaches_loopback_and_never_through_the_proxy_list() -> None:
    """The computer opens the server it just started; loopback rides `BrowserPorts.allowed` -
    never the frame's proxy allowlist."""
    frame_allowed = {"admin.example.com"}
    ports = ex_ports.ports_allowed(frame_allowed)
    assert {"127.0.0.1", "localhost"} <= ports
    assert frame_allowed == {"admin.example.com"}, "the frame's own list is never widened"


async def test_a_computer_widen_never_hands_loopback_to_the_proxy() -> None:
    from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
    from pydantic_ai.messages import TextPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from prismoid.ai.tools.browser import BROWSER_TOOLS, BrowserPorts, browser_toolset

    frame = _AllowFrame()

    async def sidecar(cmd: dict[str, Any]) -> dict[str, Any]:
        frame.verbs.append(cmd)
        return {"ok": True, "url": str(cmd.get("url", "")), "title": "t"}

    ports = BrowserPorts(
        browse=sidecar,
        allow=frame.allow,
        allowed=ex_ports.ports_allowed(set()),
    )
    toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))
    assert toolset is not None
    urls = ["http://127.0.0.1:3000/", "https://docs.example.com/"]

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if len(seen) < len(urls):
            return ModelResponse(
                parts=[ToolCallPart(tool_name="browser_open", args={"url": urls[len(seen)]})]
            )
        return ModelResponse(parts=[TextPart(str(seen[0].content))])

    agent = Agent(FunctionModel(script), output_type=[str, DeferredToolRequests], toolsets=[toolset])
    first = await agent.run("build")
    assert isinstance(first.output, DeferredToolRequests), "a public host still asks"
    assert frame.verbs[0] == {"verb": "open", "url": "http://127.0.0.1:3000/", "snapshot": True}, (
        "loopback opened unasked"
    )
    done = await agent.run(
        message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(
            approvals={first.output.approvals[0].tool_call_id: ToolApproved()}
        ),
    )
    assert isinstance(done.output, str) and done.output.startswith("ok")
    assert frame.allowed == [["docs.example.com"]], "loopback never reaches the proxy list"


async def test_a_page_reset_is_told_once_with_the_first_browser_result() -> None:
    """A park that outlasted its hold woke on a restarted browser. The resumed
    call's result ends with the notice, its first line still its own; the next one does not
    repeat it."""
    from pydantic_ai import Agent
    from pydantic_ai.messages import TextPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from prismoid.ai.tools.browser import BROWSER_TOOLS, PAGE_RESET, BrowserPorts, browser_toolset

    frame = _AllowFrame()

    async def sidecar(cmd: dict[str, Any]) -> dict[str, Any]:
        frame.verbs.append(cmd)
        return {"ok": True, "url": "https://admin.example.com/", "title": "Admin", "text": "tree"}

    ports = BrowserPorts(browse=sidecar, allow=frame.allow, allowed={"admin.example.com"}, page_reset=True)
    toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))
    assert toolset is not None
    calls = ["browser_back", "browser_snapshot"]
    returns: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if len(seen) < len(calls):
            return ModelResponse(parts=[ToolCallPart(tool_name=calls[len(seen)], args={})])
        returns.extend(str(p.content) for p in seen)
        return ModelResponse(parts=[TextPart("done")])

    await Agent(FunctionModel(script), toolsets=[toolset]).run("resume")
    assert returns[0].startswith("ok") and returns[0].endswith("\n\n" + PAGE_RESET), returns[0]
    assert PAGE_RESET not in returns[1], "told once, not on every result"
    assert ports.page_reset is False


async def test_the_journal_stamps_every_tool_call_with_its_step_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from prismoid.operations.execution import taps
    from prismoid.schemas.events import ToolCalled

    made: list[Any] = []
    workspaces: list[Any] = []

    async def record(task_id: Any, make: Any, workspace_id: Any = None) -> int:
        made.append(make(7))
        workspaces.append(workspace_id)
        return 7

    monkeypatch.setattr(taps, "record", record)

    class _T:
        id = uuid.uuid4()
        project_id = None
        workspace_id = uuid.uuid4()

    class _S:
        id = uuid.uuid4()
        attempt = 3

    journal = taps.tool_journal(cast(AsyncSession, None), cast(Task, _T()), cast(Any, _S()))
    await journal("browser.click", True, "x", 1, "", "call-1")
    assert isinstance(made[0], ToolCalled) and made[0].attempt == 3
    assert workspaces == [_T.workspace_id], "the frame's own session acts for the task's workspace (ADR-061)"
    assert ToolCalled.model_validate({**made[0].model_dump(), "attempt": None}).attempt is None  # old rows


def test_the_recording_names_the_attempt_it_came_from() -> None:
    from prismoid.operations.verification import recording_attempt

    assert recording_attempt("/artifacts/recording-pub-2-a1b2c3d4") == 2
    assert recording_attempt("recording-pub-11-deadbeef.tar") == 11
    assert recording_attempt("/artifacts/recording-1") is None
    assert recording_attempt("/artifacts/report.md") is None


async def test_a_cancelled_browser_park_publishes_its_recording_as_working_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minor (whole-branch review): the recording covers "success, failure, cancel or steer" -
    a parked browser step whose gate is cancelled binds its frame again (the Machine's, or a
    pre-1b park's by its old name: `machines.parked_frame`), publishes the trail
    (`working`: it delivered nothing), and only then is released. A frame that is gone
    publishes nothing."""
    import uuid

    from prismoid.operations import machines

    seen: list[tuple[str, Any]] = []
    parked: list[object | None] = [object()]

    async def fake_parked_frame(session: Any, task: Any, step: Any, *, profile: str) -> object | None:
        seen.append(("parked_frame", profile))
        return parked[0]

    async def fake_evidence(
        session: Any, task: Any, step: Any, frame: Any, deps: Any, **kw: Any
    ) -> list[Any]:
        seen.append(("evidence", (frame, deps, kw["success"], kw["own_output"])))
        return []

    monkeypatch.setattr(machines, "parked_frame", fake_parked_frame)
    monkeypatch.setattr(ex_harvest, "record_evidence", fake_evidence)

    class _T:
        id = uuid.uuid4()

    class _S:
        id = uuid.uuid4()
        attempt = 2

    await ex_giveback.cancelled_browser_evidence(cast(AsyncSession, None), cast(Task, _T()), cast(Any, _S()))
    assert [k for k, _ in seen] == ["parked_frame", "evidence"]
    assert seen[0][1] == "browser"
    assert seen[1][1] == (parked[0], None, False, False)

    seen.clear()
    parked[0] = None  # the parked frame is gone
    await ex_giveback.cancelled_browser_evidence(cast(AsyncSession, None), cast(Task, _T()), cast(Any, _S()))
    assert [k for k, _ in seen] == ["parked_frame"], "no frame, no recording"


def _manifest_entry(path: str, type_: str) -> dict[str, Any]:
    return {
        "path": path,
        "type": type_,
        "title": "t",
        "sha256": "0" * 64,
        "bytes": 1,
        "sources": [],
        "agent": "maker",
        "model": "m",
        "skill_pins": [],
        "at": "2026-01-01T00:00:00Z",
    }


class _ManifestFrame:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self.read: list[str] = []

    async def manifest(self) -> dict[str, Any]:
        return {"entries": self._entries}

    async def read_bytes(self, path: str) -> bytes:
        self.read.append(path)
        return b"bytes"


async def test_ordinary_harvest_ignores_an_agent_published_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `recording` entry can only be legitimate when `_record_evidence` harvests it by name
    (`only={path}`, right after `finalize`). The ordinary harvest (`only is None`, every other
    call site) must never mint an artifact from one - the publish tool already refuses to write
    it, but a forged manifest entry (a stale frame, a bypassed toolset) must not be harvestable
    through this door either."""
    import uuid

    from prismoid.operations import artifacts as artifact_ops
    from prismoid.schemas.artifacts import Manifest

    entries = [
        _manifest_entry("/artifacts/report.md", "document"),
        _manifest_entry("/artifacts/recording-1", "recording"),
    ]
    frame = _ManifestFrame(entries)

    captured: dict[str, Any] = {}

    async def fake_harvest(
        session: Any, task_id: Any, step_id: Any, manifest: Manifest, payloads: Any, **kw: Any
    ) -> list[Any]:
        captured["manifest"] = manifest
        captured["payloads"] = payloads
        return []

    async def fake_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    monkeypatch.setattr(artifact_ops, "harvest", fake_harvest)
    monkeypatch.setattr(ex_harvest, "extract_previews", fake_previews)

    class _T:
        id = uuid.uuid4()

    class _S:
        id = uuid.uuid4()

    result = await ex_harvest.harvest(cast(AsyncSession, None), cast(Task, _T()), cast(Any, _S()), frame)

    assert result == []
    kept_paths = {e.path for e in captured["manifest"].entries}
    assert kept_paths == {"/artifacts/report.md"}
    assert "/artifacts/recording-1" not in frame.read, "the forged recording's bytes were never read"


async def test_only_scoped_harvest_still_lands_the_engines_own_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_record_evidence`'s own `only={path}` call is the one legitimate way a `recording`
    entry reaches an artifact row - the filter above must not touch it."""
    import uuid

    from prismoid.operations import artifacts as artifact_ops
    from prismoid.schemas.artifacts import Manifest

    entries = [_manifest_entry("/artifacts/recording-1", "recording")]
    frame = _ManifestFrame(entries)

    captured: dict[str, Any] = {}

    async def fake_harvest(
        session: Any, task_id: Any, step_id: Any, manifest: Manifest, payloads: Any, **kw: Any
    ) -> list[Any]:
        captured["manifest"] = manifest
        return []

    async def fake_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    monkeypatch.setattr(artifact_ops, "harvest", fake_harvest)
    monkeypatch.setattr(ex_harvest, "extract_previews", fake_previews)

    class _T:
        id = uuid.uuid4()

    class _S:
        id = uuid.uuid4()

    await ex_harvest.harvest(
        cast(AsyncSession, None),
        cast(Task, _T()),
        cast(Any, _S()),
        frame,
        only=frozenset({"/artifacts/recording-1"}),
    )

    kept_paths = {e.path for e in captured["manifest"].entries}
    assert kept_paths == {"/artifacts/recording-1"}
    assert frame.read == ["/artifacts/recording-1"]
