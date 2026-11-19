# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks events` in a shared Project (ADR-062): every line names who did it, and the
presence frames read as presence. The renderer is a pure function of the frame, so it is tested
as one."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from prismoid.runner import stream
from prismoid.runner.stream import _render  # pyright: ignore[reportPrivateUsage]

ADA = {"type": "user", "id": "a-1", "name": "Ada"}
BOB = {"type": "user", "id": "a-2", "name": "Bob"}


def _presence(kind: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "lane": "live", "conn": "conn-1234", "actor": BOB, "editor": True, **extra}


def test_a_durable_event_names_its_actor_after_the_kind() -> None:
    line = _render({"type": "plan.edited", "seq": 7, "actor": ADA, "edit": {}})
    assert line.startswith("[  7] plan.edited (Ada)")


def test_an_event_without_an_actor_renders_as_before() -> None:
    line = _render({"type": "task.created", "seq": 1, "goal": "ship it", "actor": None})
    assert line == f"[  1] {'task.created':<22} ship it"
    assert "(" not in line


def test_an_actor_beside_a_body_keeps_the_body() -> None:
    line = _render({"type": "task.delivered", "seq": 9, "actor": ADA, "summary": "done"})
    assert "task.delivered (Ada)" in line and line.endswith("done")


def test_presence_join_names_the_person() -> None:
    line = _render(_presence("presence.join"))
    assert "presence.join" in line and line.endswith("Bob")
    assert "(Bob)" not in line  # the body already says who; the kind column does not repeat it


def test_a_viewer_is_shown_as_watching() -> None:
    for kind in ("presence.join", "presence.here", "presence.leave"):
        line = _render(_presence(kind, editor=False))
        assert line.endswith("Bob (watching)"), line


def test_presence_has_no_seq_and_still_renders() -> None:
    assert _render(_presence("presence.here")).startswith("[  -] presence.here")


def test_typing_steering_and_answering_read_differently() -> None:
    """The web's words: "Ada is typing…", "Bob is steering…", "Cy is answering…"."""
    assert _render(_presence("presence.typing", on=True, mode="follow_up")).endswith("Bob typing…")
    assert _render(_presence("presence.typing", on=True, mode="answer")).endswith("Bob answering…")
    assert _render(_presence("presence.typing", on=True, mode="steer")).endswith("Bob steering…")


def test_unicode_spaces_read_as_spaces_not_as_nothing() -> None:
    """A no-break or em space is a project, not a control: it prints as one instead of gluing the
    words either side together, in a body and in a name alike."""
    nbsp = {"type": "user", "id": "a-4", "name": "Ada\u00a0Lovelace"}
    line = _render({"type": "task.created", "seq": 5, "goal": "ship\u2003it\u3000now", "actor": nbsp})
    assert "ship it now" in line and "(Ada Lovelace)" in line
    zero_width = _render({"type": "task.created", "seq": 6, "goal": "ship\u200bit"})
    assert zero_width.endswith("shipit"), "a zero-width mark stays dropped: it hides text"


def test_a_question_names_its_gate_so_a_teammate_can_answer_it() -> None:
    """An editor following a Project task answers from `prismoid gates resolve <gate>`: the line says
    which gate, beside the question's title."""
    line = _render({"type": "gate.opened", "seq": 8, "gate_id": "g-42", "title": "Which currency?"})
    assert line.endswith("Which currency? · gate g-42")


def test_typing_off_says_stopped() -> None:
    assert _render(_presence("presence.typing", on=False, mode="steer")).endswith("Bob stopped")


def test_a_name_cannot_smuggle_terminal_controls() -> None:
    """Another person's display name reaches your terminal: an escape sequence in it must not."""
    evil = {"type": "user", "id": "a-3", "name": "Eve\x1b[2J\x07"}
    line = _render({"type": "plan.edited", "seq": 3, "actor": evil, "edit": {}})
    assert "\x1b" not in line and "\x07" not in line and "Eve" in line
    joined = _render({**_presence("presence.join"), "actor": evil})
    assert "\x1b" not in joined and "\x07" not in joined


def test_a_teammates_body_cannot_smuggle_terminal_controls() -> None:
    """A goal, a narration, a summary: in a shared Project any member wrote (or steered) them, and
    they print to YOUR terminal - C0/C1 controls, CSI sequences and bidi overrides are dropped,
    a line break reads as a project, and the words survive."""
    csi = "\x1b[2J\x1b]0;pwned\x07"
    c1_and_bidi = "\x9b31m\u202e\u2066"
    for frame in (
        {"type": "task.created", "seq": 1, "goal": f"ship{csi} it{c1_and_bidi}", "actor": ADA},
        {"type": "step.narration", "seq": 2, "markdown": f"ship{csi} it{c1_and_bidi}"},
        {"type": "task.delivered", "seq": 3, "summary": f"ship{csi} it{c1_and_bidi}"},
    ):
        line = _render(frame)
        assert not re.search(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", line), repr(line)
        assert "ship" in line and "it" in line
    two_lines = _render({"type": "task.created", "seq": 4, "goal": "line one\nline two"})
    assert two_lines.endswith("line one line two")


# ── steering: what a steer did, said on its own line ──────────────────────────────────────────


def test_a_steer_shows_its_words() -> None:
    line = _render(
        {"type": "plan.edited", "seq": 5, "actor": BOB, "edit": {"kind": "steer", "reason": "use euros"}}
    )
    assert line.startswith("[  5] plan.edited (Bob)") and line.endswith("use euros")


def test_an_applied_steer_names_its_author_the_step_and_the_safe_point() -> None:
    line = _render(
        {
            "type": "steer.applied",
            "seq": 6,
            "author": "Bob",
            "step_title": "Draft the memo",
            "safe_point": "before_tool",
        }
    )
    assert line.endswith("Bob's steer applied to 'Draft the memo' before its next tool call")
    replanned = _render({"type": "steer.applied", "seq": 7, "author": "Bob", "safe_point": "replan"})
    assert replanned.endswith("Bob's steer applied to 'the plan' (re-planned the remaining steps)")
    nameless = _render({"type": "steer.applied", "seq": 8, "step_title": "Draft", "safe_point": "unknown"})
    assert nameless.endswith("a steer applied to 'Draft'")


def test_every_safe_point_has_words() -> None:
    from prismoid.schemas.steering import SafePoint

    assert set(stream.APPLIED_WORDS) == {p.value for p in SafePoint} - {"boundary"}


def test_a_rejected_steer_says_why_and_a_retracted_one_says_so() -> None:
    rejected = _render(
        {"type": "steer.rejected", "seq": 9, "reason": "the task was cancelled before this applied"}
    )
    assert rejected.endswith("the task was cancelled before this applied")
    retracted = _render({"type": "steer.retracted", "seq": 10, "actor": BOB, "command_id": "c-1"})
    assert retracted.startswith("[ 10] steer.retracted (Bob)") and retracted.endswith("retracted")


def test_a_pause_says_where_it_took_hold() -> None:
    at_boundary = _render({"type": "pause.applied", "seq": 11, "safe_point": "boundary"})
    assert at_boundary.endswith("paused at the boundary")
    mid_step = _render({"type": "pause.applied", "seq": 12, "safe_point": "before_tool"})
    assert mid_step.endswith("paused before its next tool call")


def test_a_status_line_names_the_person_and_the_status() -> None:
    """D5: who paused and who resumed reads off the status line itself, prefix as before."""
    paused = _render({"type": "task.status", "seq": 13, "status": "paused", "actor": ADA})
    assert paused == f"[ 13] {'task.status (Ada)':<22} paused"
    resumed = _render({"type": "task.status", "seq": 15, "status": "running", "actor": BOB})
    assert resumed.startswith("[ 15] task.status (Bob)") and resumed.endswith("running")
    assert _render({"type": "task.status", "seq": 16, "status": "delivered"}).endswith(" delivered")


def test_the_pause_help_says_where_a_pause_takes_hold() -> None:
    """D6: a pause parks the running step at its next tool call, or holds at the boundary."""
    from typer.testing import CliRunner

    from prismoid.runner.main import app

    out = " ".join(CliRunner().invoke(app, ["tasks", "pause", "--help"]).output.split())
    assert "the running step parks at its next tool call" in out
    assert "holds at the next boundary" in out and "Stop dispatching" not in out


# ── follow_events: which presence frames become lines ────────────────────────────────────────
#
# Every stream hears every frame on the task, its own included, and a `here` is a heartbeat every
# 10 s. Rendered as-is, `prismoid do --wait` on a solo task would print "presence.here Ada" for
# its own terminal all the way through. The record is never filtered; presence is state.


def _sse(*frames: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for f in frames:
        lines += [f"event: {f['type']}", f"data: {json.dumps(f)}", ""]
    return lines


def _kinds(lines: list[str]) -> list[str]:
    return [ln.split("] ", 1)[1].split()[0] for ln in lines]  # past the "[ seq]" column


def _follow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    frames: list[dict[str, Any]],
    **kw: Any,
) -> tuple[list[str], dict[str, Any]]:
    """Run `follow_events` over canned frames; the recorded query and the printed lines."""
    asked: dict[str, Any] = {}

    def fake_stream(path: str, *, params: dict[str, Any] | None = None, **_: Any) -> Iterator[str]:
        asked.update(params or {})
        yield from _sse(*frames)

    monkeypatch.setattr(stream.client, "stream_sse", fake_stream)
    stream.follow_events("t-1", **kw)
    return [ln for ln in capsys.readouterr().out.splitlines() if ln], asked


def test_a_follow_stream_names_its_own_conn_and_hides_its_own_frames(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    own: list[str] = []

    def fake_stream(path: str, *, params: dict[str, Any] | None = None, **_: Any) -> Iterator[str]:
        conn = str((params or {})["conn"])
        own.append(conn)
        yield from _sse(
            _presence("presence.join", conn=conn, actor=ADA),
            _presence("presence.join"),
            _presence("presence.here", conn=conn, actor=ADA),
            _presence("presence.leave", conn=conn, actor=ADA),
        )

    monkeypatch.setattr(stream.client, "stream_sse", fake_stream)
    stream.follow_events("t-1", follow=True, json_out=False)
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if ln]
    assert "presence.join" in line and line.endswith("Bob")  # Bob's, not this terminal's own Ada
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", own[0])  # the shape the server accepts as a conn


def test_a_heartbeat_is_news_only_the_first_time(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    frames = [_presence("presence.here")] * 3 + [_presence("presence.typing", on=True, mode="steer")]
    lines, _ = _follow(monkeypatch, capsys, frames, follow=True, json_out=False)
    assert _kinds(lines) == ["presence.here", "presence.typing"]


def test_a_join_and_a_leave_always_print_and_a_leave_makes_the_next_here_news_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    frames = [
        _presence("presence.join"),
        _presence("presence.here"),
        _presence("presence.leave"),
        _presence("presence.here"),
    ]
    lines, _ = _follow(monkeypatch, capsys, frames, follow=True, json_out=False)
    assert _kinds(lines) == ["presence.join", "presence.leave", "presence.here"]


def test_json_mode_prints_presence_raw(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    frames = [_presence("presence.here")] * 2
    lines, _ = _follow(monkeypatch, capsys, frames, follow=True, json_out=True)
    assert [json.loads(ln) for ln in lines] == frames


def test_the_record_is_never_filtered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = {"type": "plan.edited", "seq": 4, "actor": ADA, "edit": {}}
    lines, _ = _follow(monkeypatch, capsys, [edited, edited], follow=True, json_out=False)
    assert len(lines) == 2 and all("plan.edited (Ada)" in ln for ln in lines)


def test_a_steps_live_deltas_print_no_line_and_the_record_after_them_does(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tool's live start and end were blank `[  -] activity` lines between the record's; its
    `tool.called` says what they did. Presence and the screen are live too, and still print."""
    live = {"lane": "live", "after_seq": 13, "channel": "s-1"}
    frames = [
        {"type": "activity", "kind": "tool", "phase": "start", "tool": "frame.shell", **live},
        {"type": "progress", "phase": "thinking", "elapsed_s": 4.0, **live},
        {"type": "message.delta", "text": "Hel", **live},
        {"type": "activity", "kind": "tool", "phase": "end", "ok": True, "duration_ms": 230, **live},
        _tool(seq=14, tool="frame.shell", summary="python3 --version", duration_ms=230),
        {"type": "screen.action", "lane": "live", "verb": "open", "host": "docs.python.org", "path": "/3/"},
        _presence("presence.join"),
    ]
    lines, _ = _follow(monkeypatch, capsys, frames, follow=True, json_out=False)
    assert _kinds(lines) == ["tool.called", "screen.action", "presence.join"]
    raw, _ = _follow(monkeypatch, capsys, frames, follow=True, json_out=True)
    assert [json.loads(ln) for ln in raw] == frames, "--json keeps every frame"


def test_a_finite_replay_sends_no_conn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, asked = _follow(monkeypatch, capsys, [], follow=False, json_out=False)
    assert "conn" not in asked and asked["follow"] == "false"


def test_the_events_command_takes_follow_and_no_follow(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tasks events <id> --no-follow --json` is how the real run spells a finite replay;
    both spellings reach follow_events, and the default is the finite one."""
    from typer.testing import CliRunner

    from prismoid.runner.main import app

    seen: list[bool] = []
    monkeypatch.setattr(stream, "follow_events", lambda _t, **kw: seen.append(kw["follow"]))
    for args, want in ((["--no-follow"], False), (["--follow"], True), (["-f"], True), ([], False)):
        r = CliRunner().invoke(app, ["tasks", "events", "t-1", *args, "--json"])
        assert r.exit_code == 0, r.output
        assert seen.pop() is want, args


# ── `prismoid do --wait`: which gates prompt ─────────────────────────────────────────────────


def test_a_pause_gate_renders_and_never_prompts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PAUSE gate is nobody's to answer (the gates door refuses it with a 409) - the
    task's resume lifts it. Under `--wait` it is a line, never an allow/skip prompt."""
    prompted: list[str] = []
    monkeypatch.setattr(stream.questions, "interactive", lambda: True)
    monkeypatch.setattr(stream.questions, "ask_decision", lambda gate_id, **_: prompted.append(gate_id))
    paused = {"type": "gate.opened", "seq": 7, "gate_id": "g-pause", "kind": "pause", "title": "Paused"}
    approve = {"type": "gate.opened", "seq": 8, "gate_id": "g-allow", "kind": "approve", "title": "Allow?"}

    lines, _ = _follow(monkeypatch, capsys, [paused, approve], follow=True, json_out=False, answer=True)

    assert prompted == ["g-allow"], "only the answerable gate prompts"
    assert _kinds(lines) == ["gate.opened"]


def test_a_control_gate_is_never_prompted_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nobody accepts a CONTROL gate by hand - a person takes control from the screen
    and hands back. `prismoid do --wait` prints it as a line and never offers allow/skip."""
    prompted: list[str] = []
    monkeypatch.setattr(stream.questions, "interactive", lambda: True)
    monkeypatch.setattr(stream.questions, "ask_decision", lambda gate_id, **_: prompted.append(gate_id))
    opened = {
        "type": "gate.opened",
        "seq": 5,
        "gate_id": "g-1",
        "kind": "control",
        "title": "Ada took control of the browser",
        "questions": None,
    }
    lines, _ = _follow(monkeypatch, capsys, [opened], follow=True, json_out=False, answer=True)
    assert prompted == [] and _kinds(lines) == ["gate.opened"]


def test_events_render_screen_state_with_a_body() -> None:
    held = {
        "type": "screen.state",
        "seq": 3,
        "holder": {"id": "a1", "name": "Ada"},
        "private": True,
        "until": "T",
    }
    assert _render(held).endswith("Ada (private) until T")
    assert _render({"type": "screen.state", "seq": 4, "holder": None}).endswith("nobody")


def test_a_step_frame_says_its_ending_unless_it_is_plainly_done() -> None:
    base = {"type": "step.state", "seq": 3, "title": "Open it", "state": "done"}
    assert _render(base).endswith("Open it → done")
    assert _render({**base, "outcome": "done"}).endswith("Open it → done")
    assert _render({**base, "outcome": "partial"}).endswith("Open it → done (partial)")
    blocked = {**base, "state": "escalated", "outcome": "blocked", "blocked_by": "login"}
    assert _render(blocked).endswith("Open it → escalated (blocked: login)")


def test_a_type_without_a_row_prints_its_kind_and_no_body() -> None:
    assert _render({"type": "brand.new", "seq": 2, "goal": "unseen"}).rstrip() == "[  2] brand.new"


def test_every_terminal_type_renders_a_line_from_a_bare_frame() -> None:
    for kind in stream.TERMINAL:
        assert _render({"type": kind, "seq": 1}).startswith(f"[  1] {kind}")


def test_sse_frames_pairs_each_data_line_with_the_last_event_name() -> None:
    lines = [
        ": heartbeat",
        'data: {"seq": 1}',
        "event: task.created",
        "id: 2",
        'data: {"seq": 2}',
        "",
        'data: {"seq": 3}',
        "event: task.delivered",
        'data: {"seq": 4}',
    ]
    assert list(stream.sse_frames(lines)) == [
        (None, {"seq": 1}),
        ("task.created", {"seq": 2}),
        ("task.created", {"seq": 3}),
        ("task.delivered", {"seq": 4}),
    ]


@pytest.mark.parametrize(
    ("frame", "prompt"),
    [
        ({"type": "gate.opened", "kind": "pause", "gate_id": "g"}, None),
        ({"type": "gate.opened", "kind": "control", "source": "takeover", "gate_id": "g"}, None),
        ({"type": "step.state", "kind": "budget", "gate_id": "g"}, None),
        ({"type": "gate.opened", "kind": "budget", "gate_id": "g"}, "ask_budget"),
        ({"type": "gate.opened", "kind": "input", "questions": {"q": 1}, "gate_id": "g"}, "ask"),
        ({"type": "gate.opened", "kind": "approve", "gate_id": "g"}, "ask_decision"),
    ],
)
def test_each_gate_frame_opens_its_own_prompt_or_none(
    monkeypatch: pytest.MonkeyPatch, frame: dict[str, Any], prompt: str | None
) -> None:
    asked: list[str] = []
    for name in ("ask_budget", "ask", "ask_decision", "ask_done"):
        monkeypatch.setattr(stream.questions, name, lambda *a, _n=name, **kw: asked.append(_n))
    monkeypatch.setattr(stream.questions, "interactive", lambda: True)
    monkeypatch.setattr(stream.client, "stream_sse", lambda *a, **kw: iter([f"data: {json.dumps(frame)}"]))
    monkeypatch.setattr(stream.typer, "secho", lambda *a, **kw: asked.append("line"))
    stream.follow_events("t1", follow=False, json_out=False, answer=True)
    assert asked == [prompt or "line"]


@pytest.mark.parametrize(
    ("event_name", "frame", "ends"),
    [
        ("task.delivered", {"type": "task.delivered"}, True),
        # `prismoid tasks cancel` on a budget-parked task writes only `task.status{cancelled}`
        # (nothing emits `task.cancelled`), so `prismoid do --wait` must end on it, never sit on
        # the live stream after the task has stopped. A failed task is the same.
        ("task.status", {"type": "task.status", "status": "cancelled"}, True),
        ("task.status", {"type": "task.status", "status": "failed"}, True),
        ("task.status", {"type": "task.status", "status": "running"}, False),
        ("task.status", {"type": "task.status", "status": "waiting"}, False),
        ("step.state", {"type": "step.state", "state": "failed"}, False),
    ],
)
def test_a_stopped_task_ends_the_follow(event_name: str, frame: dict[str, Any], ends: bool) -> None:
    assert stream.ends_follow(event_name, frame) is ends


def _tool(**extra: Any) -> dict[str, Any]:
    return {"type": "tool.called", "seq": 15, "tool": "browser.open", "ok": True, "duration_ms": 0, **extra}


def test_a_tool_line_names_the_tool_what_it_touched_and_how_long_it_took() -> None:
    assert _render(_tool(summary="www.notion.com/pricing", duration_ms=1840)).endswith(
        "browser.open www.notion.com/pricing · 1.8s"
    )
    assert _render(_tool(tool="frame.read_file", summary="/skills/x.md", duration_ms=8)).endswith(
        "frame.read_file /skills/x.md · 8ms"
    )
    assert _render(_tool(tool="web.search", duration_ms=125_000)).endswith("web.search · 2m 05s")


def test_a_failed_tool_line_says_why() -> None:
    failed = _tool(
        ok=False,
        summary="www.notion.com/pricing",
        duration_ms=30_002,
        denied_reason="BrowserFailed: timed out",
    )
    assert _render(failed).endswith(
        "browser.open www.notion.com/pricing - failed: BrowserFailed: timed out · 30.0s"
    )
    assert _render(_tool(ok=False)).endswith("browser.open - failed: no reason given")


def test_a_long_tool_summary_is_cut_with_a_mark_and_the_ending_survives() -> None:
    line = _render(_tool(tool="frame.shell", summary="x" * 500, duration_ms=12))
    assert "x" * 119 + "…" in line and line.endswith(" · 12ms")


def test_a_usage_line_rounds_the_price_and_names_what_was_billed() -> None:
    line = _render(
        {
            "type": "usage.recorded",
            "seq": 3,
            "model": "claude-haiku-5-5",
            "cost_usd": "0.022358000000",
            "units": {"request": 1, "input_token": 14, "output_token": 39, "cache_write_token": 3000},
        }
    )
    assert line.endswith("claude-haiku-5-5 $0.0224 · 14 in, 39 out, 3.0k cache write")


def test_a_usage_line_never_rounds_a_real_cost_to_zero() -> None:
    def priced(cost: str, units: dict[str, int]) -> str:
        return _render({"type": "usage.recorded", "seq": 1, "model": "m", "cost_usd": cost, "units": units})

    assert priced("0.000000300000", {}).endswith("m <$0.0001")
    assert priced("0E-12", {"second": 605}).endswith("m $0 · 10m 05s")
    assert priced("0.01", {"search": 1}).endswith("m $0.0100 · 1 search")
    assert priced("n/a", {}).endswith("m $n/a"), "an unreadable price prints as it came"


def test_a_step_prompt_line_names_the_agent_and_model_never_the_text() -> None:
    frame = {
        "type": "step.prompt",
        "seq": 13,
        "attempt": 2,
        "agent": "computer",
        "prompt": "the creator's private memory",
        "model_ref": "anthropic:claude-sonnet-5-5",
        "truncated": True,
    }
    line = _render(frame)
    assert line.endswith("attempt 2 · computer on claude-sonnet-5-5 (clipped)")
    assert "private memory" not in line


def test_a_plan_line_says_the_route_the_steps_and_who_chose() -> None:
    plan = {"steps": [{"id": "s1"}, {"id": "s2"}]}
    frame = {"type": "task.planned", "seq": 8, "plan": plan, "route": "standard", "route_source": "planner"}
    assert _render({**frame, "machine": "computer"}).endswith("standard · 2 steps (planner) · computer")
    one = {**frame, "plan": {"steps": [{"id": "s1"}]}, "machine": "none", "fallback": "planner timed out"}
    assert _render(one).endswith("standard · 1 step (planner) · fallback: planner timed out")


def test_an_artifact_line_says_its_kind_title_and_size() -> None:
    frame = {
        "type": "artifact.produced",
        "seq": 44,
        "artifact_type": "document",
        "title": "Prices",
        "bytes": 4200,
    }
    assert _render(frame).endswith('document "Prices" · 4.1 KB')
    assert _render({**frame, "bytes": 900, "role": "working"}).endswith('document "Prices" · 900 B (working)')


def test_an_answered_gate_says_the_action_and_never_the_answer() -> None:
    frame = {
        "type": "gate.resolved",
        "seq": 21,
        "gate_id": "g-42",
        "action": "accept",
        "content": {"q1": "my salary"},
    }
    line = _render(frame)
    assert line.endswith("accept · gate g-42") and "salary" not in line


@pytest.mark.parametrize(
    ("frame", "body"),
    [
        ({"type": "task.dequeued", "turn": 2}, "turn 2"),
        ({"type": "task.renamed", "title": "iPhone prices"}, "iPhone prices"),
        ({"type": "task.continued", "message": "and in euros?", "turn": 2}, "and in euros?"),
        ({"type": "machine.created", "profile": "browser"}, "browser profile"),
        ({"type": "machine.slept", "reason": "idle", "after_s": 605}, "idle · after 10m 05s"),
        (
            {"type": "verification.signed", "verdict": "pass", "suite": "document-suite"},
            "pass (document-suite)",
        ),
        (
            {"type": "escalation.raised", "diagnosis": {"what_failed": "the site was down"}},
            "the site was down",
        ),
        (
            {"type": "artifact.revised", "artifact_type": "document", "title": "Note", "revision": 2},
            'document "Note" · revision 2',
        ),
        (
            {"type": "context.assembled", "total_tokens": 7800, "memory_ids": ["m-1"]},
            "7.8k tokens · 1 memory",
        ),
        ({"type": "memory.written", "count": 0}, "kept 0 memories"),
        (
            {"type": "memory.written", "count": 0, "skipped": "project"},
            "none: a project task keeps no memory",
        ),
        ({"type": "bot_memory.written", "bot": "Scout", "count": 2}, "Scout kept 2 memories"),
    ],
)
def test_each_record_kind_says_what_happened(frame: dict[str, Any], body: str) -> None:
    assert _render({**frame, "seq": 1}).endswith(f" {body}")


def test_a_memory_line_counts_the_memories_and_never_says_them() -> None:
    items = [{"id": "m-1", "kind": "fact", "text": "my salary is 90k"}]
    line = _render({"type": "memory.written", "seq": 1, "count": 1, "items": items})
    assert line.endswith("kept 1 memory") and "salary" not in line


@pytest.mark.parametrize(
    "kind",
    [
        "tool.called",
        "usage.recorded",
        "step.prompt",
        "task.planned",
        "artifact.produced",
        "artifact.revised",
        "context.assembled",
        "memory.written",
        "machine.slept",
    ],
)
def test_a_bare_frame_of_each_new_kind_still_renders(kind: str) -> None:
    """Older rows may lack any field; the line degrades, it never raises."""
    assert _render({"type": kind, "seq": 1}).startswith(f"[  1] {kind}")
