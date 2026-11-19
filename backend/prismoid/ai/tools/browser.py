# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The browser family - the operator's browser hands.

Shaped like the web door (`fetch.py`): a toolset that takes CALLABLES (the frame's `browser`
and `allow` verbs) and values, never `TaskDeps`; journals argument keys, host and path, never
typed text or a query string; returns page text inside a data fence. Whether an act asks is the
run's approval rules' call (`approve`, `ai/rules/approvals.py`), handed what the act is; an
ask raises pydantic-ai's `ApprovalRequired` BEFORE the act, so the engine's existing approve gate
parks the step (the per-tool approval machinery, generalized by `source`).

**An approval binds to what the human approved - never to a fresh look at the page, and never
to a value the MODEL supplies.** Every acting verb (`click/type/fill/select/press`) takes a
trailing `expect` parameter that only the APPROVER may set (the engine, resuming with
`ToolApproved(override_args={..., "expect": metadata["fingerprint"]})`). The tool fails
closed on both sides of that boundary:

- **Unapproved call, any `expect` at all** (`{}`, a partial dict, a full one - it does not
  matter which): refused outright, before any inspect and before any ask. A model may never
  set the one field the whole binding depends on; a bare `ToolApproved()` replaying the
  model's original args must never let a model-authored `expect` sneak through as if it were
  the approver's.
- **Approved call, `expect` missing or not a dict carrying all four keys `role`, `name`,
  `host`, `path` as non-None strings** - for `fill`, which types into several targets, `expect`
  maps each field's ref to such a dict, and one missing or partial is the same: refused with
  the same "did not carry the fingerprint" text a wholly-missing `expect` gets - a partial or
  malformed `expect` is exactly as untrustworthy as a missing one.
- **Approved call, a fully-shaped `expect`**: this module does NOT re-inspect the target -
  re-inspecting there was the exact binding bug this contract closes: a fresh inspect answers
  whatever the page NOW shows, which may not be what the human looked at when they said yes.

Only an unapproved call with no model-supplied `expect` inspects its targets, to build the
preview and the fingerprint the ask carries; the sidecar itself refuses to act on a mismatch
between `expect` and what it finds (`intervened`), the last line of defense against the page
having moved between the ask and the act.

`browser_scroll` sits OUTSIDE this whole contract - it never inspects, never asks, and never
carries an `expect`: scrolling has no consequence to approve.

The three asks: leaving the allowlist (`browser_open` to a new host - three
scopes, once/task/always; the host must pass `computer_use.check_host` or the target is
refused outright, never offered as something to ask about), a danger word (no scopes: a
one-time yes) - checked on the target's own accessible name UNLESS its role is a text-entry
one (`TEXT_ENTRY_ROLES`: textbox, searchbox, combobox, spinbutton, textarea - a text field's
name is very often its placeholder, not a description of a consequence). Every OTHER role
contributes its name, including an empty or unrecognized one - fail closed: an icon button the
page never labelled with a role is exactly the case a silent exemption would miss. A submit
(`submit=True`) always additionally checks the submit control's own name and the form's labels
- of EVERY field a `fill` types into, since its Enter lands in the last field's form. A click
on a control that submits its form (`inspect`'s `submits`: a submit button, a button the page
gave no type, an image input) is a submit too - it sends the same form the Enter would. A
link whose click only loads another page (`inspect`'s `navigates`) is navigation, its name unread,
and a submit of a form the page sends by GET (`form_navigates`, a search) reads no form names. A
`select` is checked like a submit (its `change` event may submit the form), and its CHOICE -
the value and the option text that value picks - counts too, whatever the select's own role.
The third ask is the model's own doubt (`confirm=true` - same shape as a danger hit, and
unaffected by role).

Every acting verb is checked once, the same way: its return carries `ActCheck` (did it land, from
the sidecar's own facts, never the page's words) beside its page facts (`Acted`), and the run's
ending reads it (`act_of`). A fill's targets are inspected in one sidecar call.

Every verb says its own `tool.called` row (`call_rows.say`), because its label is the sidecar's
answer: the target the inspect found, or `browser_snapshot`'s own locator (the page it landed on),
only knowable from its own answer. A raising `ports.allow` (the allowlist widen) is journaled
rather than silently swallowed.

A person can take the browser over: before every call - `guard_action`'s inspect
included - the toolset reads `BrowserPorts.control()`, and a live lease raises `CallDeferred`
(kind `CONTROL_HELD`); `browser_handoff(reason)` raises `CallDeferred` (kind `HANDOFF`). Both
park the step on a CONTROL gate that the lease's ending answers; the parked call never runs.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

import logfire
from pydantic import BaseModel, ValidationError
from pydantic_ai import BinaryContent, CallDeferred, RunContext, ToolReturn
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools import catalog, labels
from prismoid.ai.tools.browser_policy import (
    CONTROL_HELD,
    CONTROL_UNKNOWN,
    EXPECT_NOT_MODEL_SET,
    FENCE_CLOSE,
    FENCE_OPEN,
    HANDOFF,
    HANDOFF_UNAVAILABLE,
    NO_FINGERPRINT,
    PAGE_RESET,
    SCOPES,
    TEXT_ENTRY_ROLES,
    PageState,
    act_target,
    acted_text,
    announcement,
    binding_before_inspect,
    blank_page,
    blanked,
    consent,
    ends_response,
    handoff_metadata,
    held_metadata,
    inspect_command,
    noticed,
    open_plan,
    open_state,
    opened_text,
    page_state,
    redirect_target,
    refused_state,
    reset_before,
    response_of,
    result_text,
    saved_text,
    shown,
    skipped_text,
    snapshot_summary,
    snapshot_text,
    state_of,
    unpaired_in,
    widen_failed,
    widen_hosts,
)
from prismoid.ai.tools.call_rows import DECLINED, JournalTool, Said, declined, say
from prismoid.schemas.approvals import Act, Approve, Door
from prismoid.schemas.computer_use import ACTING_VERBS, ActCheck, act_check
from prismoid.schemas.outputs import SiteBlock
from prismoid.schemas.screen import ControlState, ControlUnknown
from prismoid.schemas.side_effects import SideEffect

# The names other modules import from here; the decisions and their texts live in
# `browser_policy`, re-exported so every importer keeps one door.
__all__ = [
    "ACTING_VERBS",
    "BROWSER_TOOLS",
    "CONTROL_HELD",
    "CONTROL_UNKNOWN",
    "EXPECT_NOT_MODEL_SET",
    "FENCE_CLOSE",
    "FENCE_OPEN",
    "HANDOFF",
    "HANDOFF_UNAVAILABLE",
    "NO_FINGERPRINT",
    "PAGE_RESET",
    "SCOPES",
    "TEXT_ENTRY_ROLES",
    "TOOL_NAMES",
    "Acted",
    "BrowserFailed",
    "BrowserPorts",
    "FillField",
    "PageState",
    "act_of",
    "browser_toolset",
]

BROWSER_TOOLS: Final = tuple(tool.journal for tool in catalog.family("browser"))
_OPEN: Final = catalog.tool("browser_open")
_BACK: Final = catalog.tool("browser_back")
_WAIT: Final = catalog.tool("browser_wait")
_SNAPSHOT: Final = catalog.tool("browser_snapshot")
_CLICK: Final = catalog.tool("browser_click")
_TYPE: Final = catalog.tool("browser_type")
_FILL: Final = catalog.tool("browser_fill")
_SELECT: Final = catalog.tool("browser_select")
_PRESS: Final = catalog.tool("browser_press")
_SCROLL: Final = catalog.tool("browser_scroll")
_SCREENSHOT: Final = catalog.tool("browser_screenshot")
_HANDOFF: Final = catalog.tool("browser_handoff")
# The toolset's own journal names, never a tool a model calls: an allowlist widen or a refused save
# is journaled beside the one recorded call, never counted as a second one.
ALLOW: Final = "browser.allow"
SAVE: Final = "browser.save"
# A save asked of a computer whose browser cannot file one (`BrowserPorts.save` is None): one tool
# schema on every computer, and the difference said in the answer.
NO_SAVE: Final = "refused: this computer cannot save from the browser; ask again without save_as"


@dataclass(slots=True)
class BrowserPorts:
    """The frame's browser verbs, handed in as callables - never a row, never `TaskDeps`
    (the `fetch.py`/`connectors.py` sibling law)."""

    browse: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]  # frame.browser
    allow: Callable[[list[str]], Awaitable[bool | None]]  # frame.allow; False = dropped as stale
    allowed: set[str] = field(default_factory=set[str])  # the live allowlist; the toolset ADDS on approval
    # How many recent pages stay in full when the engine's client-side collapse fires
    # (`collapse_old_pages`) - handed through as a setting; the window itself lives one layer
    # up, in the engine's message history, never in a per-call toolset closure.
    snapshot_window: int = 2
    page_reset: bool = False  # the Machine slept during a park: the next result says so, once
    # The task's control lease, read before EVERY browser call: a live one parks the
    # call, and one that cannot be read (`ControlUnknown`) refuses it. Operations injects it (the
    # journal-callable precedent), so this module stays persistence-free. None - a seat's
    # throwaway browser, a test - means nobody can take it over.
    control: Callable[[], Awaitable[ControlState | None]] | None = None
    # Each acting verb that succeeded, for the screen: `{"verb", "url", "box"?}` -
    # never the typed text. Operations publishes it as `screen.action`; None outside a Machine.
    on_action: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None
    # `frame.save_output`, on a frame on the device: lands a screenshot and answers where. None
    # elsewhere, and a `save_as` is refused in the answer (`NO_SAVE`).
    save: Callable[[str, bytes], Awaitable[str]] | None = None
    # `frame.hand_over`, on a frame on the device: the window follows the person until the agent
    # next acts. None elsewhere.
    hand_over: Callable[[], Awaitable[None]] | None = None
    # `frame.denials`: what the frame's egress refused. Read (never drained: harvest still journals
    # them) at each open and when a page came back empty, so the model is told why. None in tests.
    denials: Callable[[], Awaitable[list[dict[str, str]]]] | None = None


# The model-facing names. Which returns are the browser's is decided by TOOL NAME, never by the
# fence: another tool's text (a fetched page, a file) can carry the fence's own words.
TOOL_NAMES: Final = frozenset(tool.name for tool in catalog.family("browser"))


class FillField(BaseModel):
    ref: str
    value: str


class Acted(PageState, frozen=True):
    """An acting verb's facts: its page's (`PageState`) and its check (`ActCheck`)."""

    act: ActCheck


class _Checked(BaseModel, frozen=True):
    """The check any acting verb's return carries, with page facts (`Acted`) or without (a refusal)."""

    act: ActCheck


def act_of(metadata: object) -> ActCheck | None:
    """The check a browser return carried, live or read back from the history; None for a call that
    did not act (a look, a refusal said before the sidecar)."""
    try:
        return _Checked.model_validate(metadata, from_attributes=True).act
    except ValidationError:  # fail-closed: facts that do not parse are no facts
        return None


def _returned(result: Mapping[str, Any], text: str, verb: str = "") -> ToolReturn[str]:
    """`text` for the model, with the facts of the answer beside it (`PageState`), when it has any,
    and an acting `verb`'s check (`Acted`; a bare `_Checked` when the answer has no page facts)."""
    state = page_state(result)
    if verb not in ACTING_VERBS:
        return ToolReturn(text, metadata=state)
    check = act_check(verb, result)
    facts = _Checked(act=check) if state is None else Acted(**state.model_dump(), act=check)
    return ToolReturn(text, metadata=facts)


class BrowserFailed(Exception):
    """A verb the sidecar refused: journaled `ok=false` with the reason (the `FetchDenied`
    precedent), then RETURNED to the model as text, never a `ModelRetry`."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


async def _announce(ports: BrowserPorts, verb: str, result: dict[str, Any]) -> None:
    """An acting verb that succeeded, to the screen. A courtesy to the people watching: it never
    fails the agent's act."""
    if ports.on_action is None or (action := announcement(verb, result)) is None:
        return
    try:
        await ports.on_action(action)
    except Exception:
        logfire.warn("the browser could not show an action on the screen; the act stands", _exc_info=True)


async def _widen(journal: JournalTool | None, ports: BrowserPorts, host: str) -> str | None:
    """Widen the live allowlist, journaled as `browser.allow`; `ports.allowed` changes only AFTER the
    widen succeeds, so a failed widen never looks granted."""
    widened, proxied = widen_hosts(ports.allowed, host)
    try:
        if await ports.allow(proxied) is False:
            # The egress proxy dropped the push as stale: the live list is unchanged.
            raise RuntimeError("the egress proxy dropped the widen as stale")
    except Exception as exc:  # fail-soft: the model hears why and the list stays as it was
        await say(journal, Said.failed(ALLOW, exc, host))
        return widen_failed(host, exc)
    await say(journal, Said(ALLOW, True, host))
    ports.allowed.clear()
    ports.allowed.update(widened)
    return None


class SaveRefused(Exception):
    """The frame's door, or the name, refused a save: its text is the tool's answer."""


async def _save(
    journal: JournalTool | None, ports: BrowserPorts, path: str, data: bytes, *, page: bool = False
) -> str:
    """`data` (a .png screenshot or a .md page) at `path` through the frame's door, journaled as
    `browser.save`; a refusal is the door's own words (`SaveRefused`)."""
    what, suffix = ("the page", ".md") if page else ("a screenshot", ".png")
    summary = f"save {what}"
    try:
        if not path.lower().endswith(suffix):
            raise ValueError(f"the name does not end in {suffix}")
        assert ports.save is not None
        saved = await ports.save(path, data)
    except Exception as exc:
        await say(journal, Said.failed(SAVE, exc, summary))
        if isinstance(exc, ConnectionError) or not isinstance(exc, OSError | ValueError):
            raise  # a frame that did not answer refused nothing: the step's to wait out
        raise SaveRefused(f"refused: {what} saves as a {suffix} in the work or out folder ({exc})") from None
    await say(journal, Said(SAVE, True, summary))
    return saved


async def _page_host(ports: BrowserPorts) -> str:
    """The page's host in the sidecar's words, or nothing for a page that cannot be read."""
    try:
        info = await ports.browse({"verb": "inspect"})
        return labels.terminal_line(str(info.get("host") or "")) if info.get("ok") else ""
    except Exception:
        logfire.warn("the browser could not read the page's host; the hand-off names none", _exc_info=True)
        return ""


@dataclass(slots=True)
class _BrowserTools:
    """The toolset's I/O: each tool reads the facts, asks `browser_policy` what they mean and acts,
    journaling each call (bound methods, so each keeps the docstring and schema the model reads)."""

    journal: JournalTool | None
    ports: BrowserPorts
    approve: Approve  # the run's approval rules (`ai/rules/approvals.py`)
    # Best effort, this run only (a resumed run starts empty): (the response, the call) whose call
    # changed the page mid-response - the acts after it are skipped. A resume starts a new response.
    moved: tuple[str, str] | None = None
    # The frame's egress refusals as they stood when the current page was opened, so an empty page
    # names only its own (`_why_blank`). None: no page opened in this run, and nothing is named.
    refused_before: list[dict[str, str]] | None = None

    async def _yield_if_held(self, ctx: RunContext[Any], tool: str) -> str | None:
        """Before every browser call dispatches, a person driving the browser parks
        this call - before `guard_action`'s inspect, before any ask, before the sidecar. The call
        never executes; on resume its result is the CONTROL gate's outcome (who drove, for how
        long). A verb already in flight is never interrupted: the sidecar drops a person's input
        while it holds the verb lock. A lease that cannot be read refuses the call instead (the
        returned text, journaled): unknown is not "nobody", and no lease would answer a gate.
        None: nobody drives, the call goes ahead."""
        if self.ports.control is None:
            return None
        try:
            held = await self.ports.control()
        except ControlUnknown:
            label = labels.browser_label(None, tool.split(".", 1)[1])
            return await self._refuse(tool, CONTROL_UNKNOWN, label)
        if held is not None:
            raise CallDeferred(metadata=held_metadata(held))
        return None

    async def _before_act(self, ctx: RunContext[Any], tool: str) -> str | None:
        """Before a verb that acts on the page the model was shown: a person driving parks it
        (`_yield_if_held`), and when an earlier call of the same response changed the page it is
        skipped - its references were the old page's. Never sent, never inspected, never asked
        about; journaled as not run. None: the call goes ahead."""
        if (refused := await self._yield_if_held(ctx, tool)) is not None:
            return refused
        if self.moved is None or self.moved[0] != response_of(ctx.messages):
            return None
        label = labels.browser_label(None, tool.split(".", 1)[1])
        return await self._refuse(tool, skipped_text(self.moved[1]), label)

    async def _call(
        self, tool: str, cmd: dict[str, Any], summary: str | Callable[[Any], str]
    ) -> dict[str, Any]:
        """Call the sidecar and journal the call; a result-dependent summary (`browser_snapshot`'s
        own location) is resolved from the actual answer rather than guessed before the call."""
        result = await self.ports.browse(cmd)
        label = summary(result) if callable(summary) else summary
        if not result.get("ok"):
            failed = BrowserFailed(str(result.get("error", "failed")), result)
            await say(self.journal, Said.failed(tool, failed, label))
        else:
            await say(self.journal, Said(tool, True, label))
            await _announce(self.ports, str(cmd.get("verb", "")), result)
        if self.ports.page_reset:  # the first result after a reset page says so (`noticed`), once
            self.ports.page_reset = False
            result = {**result, "notice": PAGE_RESET}
        return result

    async def _act(
        self, ctx: RunContext[Any], tool: str, cmd: dict[str, Any], summary: str
    ) -> ToolReturn[str]:
        """Run one verb and bring its page back, fenced like `browser_snapshot`: always for the response's
        last call, else only when it left the page (`moved`); later acts are skipped (`_before_act`)."""
        last = ends_response(ctx.messages, ctx.tool_call_id, TOOL_NAMES)
        if cmd["verb"] == "open":
            self.refused_before = await self._refusals()
        result = await self._call(tool, {**cmd, "snapshot": True if last else "moved"}, summary)
        if not last and result.get("ok") and result.get("moved"):
            self.moved = (response_of(ctx.messages) or "", catalog.model_name(tool))
        if cmd["verb"] in ("open", "wait"):
            result = await self._why_blank(result)
        return _returned(result, acted_text(result), cmd["verb"])

    async def _refusals(self) -> list[dict[str, str]] | None:
        """The frame's egress refusals: a read that journals and drains nothing.
        None when there is no list to read, or it could not be read."""
        if self.ports.denials is None:
            return None
        try:
            return await self.ports.denials()
        except Exception:
            logfire.warn("the browser could not read the frame's egress refusals", _exc_info=True)
            return None

    async def _why_blank(self, result: dict[str, Any]) -> dict[str, Any]:
        """`result` with the hosts the sandbox refused since the page was opened, when its page
        came back empty (`blanked`): the model says what blocked it, not "the page stayed blank"."""
        if self.refused_before is None or not blank_page(result) or (now := await self._refusals()) is None:
            return result
        return blanked(result, self.refused_before, now)

    async def _refuse(self, tool: str, reason: str, summary: str) -> str:
        """A refusal that never reaches (or never re-reaches) the sidecar - journaled
        `ok=false` the same way, without a sidecar round-trip."""
        await say(
            self.journal, Said.failed(tool, BrowserFailed(reason, {"ok": False, "error": reason}), summary)
        )
        return reason

    async def _inspect_all(
        self, refs: tuple[str, ...], choice: str | None
    ) -> dict[str, dict[str, Any]] | str:
        """Each distinct target's `inspect` answer, in order, in one sidecar call, or the first
        one's error. A batch answered in any other shape (an older sidecar) refuses: fail-closed."""
        targets = tuple(dict.fromkeys(refs))
        if len(targets) == 1:
            info = await self.ports.browse(inspect_command(targets[0], choice))
            return {targets[0]: info} if info.get("ok") else str(info.get("error", "failed"))
        batch = await self.ports.browse({"verb": "inspect", "refs": list(targets)})
        each = cast("list[dict[str, Any]] | None", batch.get("each") if batch.get("ok") else None)
        if not isinstance(each, list) or len(each) != len(targets):
            return str(batch.get("error", "failed: the targets could not be inspected"))
        return dict(zip(targets, each, strict=True))

    async def guard_action(
        self,
        ctx: RunContext[Any],
        *,
        tool: str,
        refs: tuple[str, ...],
        confirm: bool,
        submit: bool,
        expect: dict[str, Any] | None,
        choice: str | None = None,
        by_ref: bool = False,
    ) -> dict[str, dict[str, str]] | str:
        """The approval binding (`binding_before_inspect`), then, only when unapproved and
        `expect`-free, an inspect of every target and the danger/doubt check (`consent`).

        Returns `{ref: fingerprint}` for every target when the caller should act, or the
        reason it must refuse without acting at all - never falling through to the sidecar on
        a bad inspect, a missing/partial approval fingerprint, or a model-supplied `expect` it
        was never the model's to set. An ask raises `ApprovalRequired` before any act."""
        early = binding_before_inspect(
            approved=bool(ctx.tool_call_approved), refs=refs, expect=expect, by_ref=by_ref
        )
        if early is not None:
            return early
        infos = await self._inspect_all(refs, choice)
        if isinstance(infos, str):
            return infos
        decided = consent(tool, refs, infos, submit=submit, choice=choice, confirm=confirm, by_ref=by_ref)
        # The person already said no to this very act (its targets, not just the verb): their answer.
        if declined(ctx.messages, catalog.model_name(tool), act_target(decided.bound)):
            return DECLINED
        act = Act(door=Door.BROWSER, effect=SideEffect.WORLD, irreversible=decided.irreversible)
        refused = self.approve(act, False, decided.ask.metadata())
        return decided.bound if refused is None else refused

    async def _guarded(
        self, ctx: RunContext[Any], tool: str, label: str, **guard: Any
    ) -> dict[str, dict[str, str]] | str:
        """`guard_action`, with a refusal journaled under `label` and returned as the tool's text."""
        bound = await self.guard_action(ctx, tool=tool, **guard)
        if isinstance(bound, str):
            return await self._refuse(tool, bound, labels.browser_label(None, label))
        return bound

    async def browser_open(self, ctx: RunContext[Any], url: str) -> str | ToolReturn[str]:
        """Open a URL in the browser. Only hosts the task may reach; a new host asks the user."""
        if (refused := await self._yield_if_held(ctx, _OPEN.journal)) is not None:
            return refused
        # The bare hosts whose www. twin sent the browser back are read off the history, so a
        # resumed run never pairs them again (a loop between the twin and the ask).
        approved, unpaired = bool(ctx.tool_call_approved), unpaired_in(ctx.messages)
        plan = open_plan(url, self.ports.allowed, approved=approved, unpaired=unpaired)
        if plan.refusal is not None:
            refused = await self._refuse(_OPEN.journal, plan.refusal, labels.fetch_label(url))
            return ToolReturn(refused, metadata=refused_state(url))
        if plan.ask is not None:
            act = Act(door=Door.BROWSER, effect=SideEffect.WORLD, widens_egress=True)
            # A host the person already declined to open is answered with their no, never asked again.
            host = labels.terminal_line(plan.ask.host)  # as its card names it (`BrowserAsk.metadata`)
            no = DECLINED if declined(ctx.messages, _OPEN.name, host) else None
            if (refused := no or self.approve(act, False, plan.ask.metadata())) is not None:
                refused = await self._refuse(_OPEN.journal, refused, labels.fetch_label(url))
                return ToolReturn(refused, metadata=refused_state(url))
        if plan.widen is not None:
            failure = await _widen(self.journal, self.ports, plan.widen)
            if failure is not None:
                return failure
        cmd = {"verb": "open", "url": plan.target}
        done = await self._act(ctx, _OPEN.journal, cmd, labels.fetch_label(plan.target))
        opened, redirected = state_of(done.metadata), None
        if opened is not None and opened.blocked is SiteBlock.EGRESS:  # a redirect off the allowed site?
            redirected = redirect_target(self.refused_before, await self._refusals(), plan.host)
        again = reset_before(ctx.messages, plan.host)
        state = open_state(opened, plan, again=again, redirected=redirected)
        facts = done.metadata if state is None else state
        return ToolReturn(opened_text(str(done.return_value), state), metadata=facts)

    async def browser_back(self, ctx: RunContext[Any]) -> str | ToolReturn[str]:
        """Go back one page."""
        if (refused := await self._yield_if_held(ctx, _BACK.journal)) is not None:
            return refused
        return await self._act(ctx, _BACK.journal, {"verb": "back"}, "back")

    async def browser_wait(self, ctx: RunContext[Any], for_text: str) -> str | ToolReturn[str]:
        """Wait until a text shows on the page (up to 10 s): a result or a message that arrives
        after the page itself loaded. Every act already waits for the page to settle."""
        if (refused := await self._yield_if_held(ctx, _WAIT.journal)) is not None:
            return refused
        return await self._act(ctx, _WAIT.journal, {"verb": "wait", "for": for_text}, "wait")

    async def browser_snapshot(
        self, ctx: RunContext[Any], find: str | None = None, save_as: str | None = None
    ) -> str | ToolReturn[str]:
        """The page as an accessibility tree with [eN.M] references for interactive elements.
        `find` narrows it to the elements whose name contains that text, with what is inside
        them - for anything the full tree cut or folded. With `save_as`, a path ending in .md in
        the work or out folder, the WHOLE page is saved there as text instead - every item on one
        line, no references - for a script to read (to compare many items, compute with python3
        in the shell), and you get its path and first lines back; a computer that cannot save
        from the browser says so."""
        if (refused := await self._yield_if_held(ctx, _SNAPSHOT.journal)) is not None:
            return refused
        if save_as is None:
            cmd = {"verb": "snapshot", **({"find": find} if find else {})}
            result = await self._why_blank(await self._call(_SNAPSHOT.journal, cmd, snapshot_summary))
            return _returned(result, snapshot_text(result))
        if self.ports.save is None:
            return await self._refuse(_SNAPSHOT.journal, NO_SAVE, "save the page")
        result = await self._call(_SNAPSHOT.journal, {"verb": "snapshot", "whole": True}, snapshot_summary)
        if not result.get("ok"):
            return noticed(result, result_text(result))
        text = str(result.get("text", "")).encode()
        try:
            path = await _save(self.journal, self.ports, save_as, text, page=True)
        except SaveRefused as refused:
            return noticed(result, str(refused))
        return _returned(result, noticed(result, saved_text(path, result)))

    async def browser_click(
        self, ctx: RunContext[Any], ref: str, confirm: bool = False, expect: dict[str, str] | None = None
    ) -> str | ToolReturn[str]:
        """Click an element by reference. Set confirm=true when the consequence is unclear."""
        if (refused := await self._before_act(ctx, _CLICK.journal)) is not None:
            return refused
        bound = await self._guarded(
            ctx, _CLICK.journal, "click", refs=(ref,), confirm=confirm, submit=False, expect=expect
        )
        if isinstance(bound, str):
            return bound
        fp = bound[ref]
        cmd = {"verb": "click", "ref": ref, "expect": fp}
        return await self._act(ctx, _CLICK.journal, cmd, labels.browser_label(shown(fp), "click"))

    async def browser_type(
        self,
        ctx: RunContext[Any],
        ref: str,
        text: str,
        submit: bool = False,
        confirm: bool = False,
        *,
        expect: dict[str, str] | None = None,
    ) -> str | ToolReturn[str]:
        """Type into a field by reference; submit=true presses Enter afterwards. A date or time
        field takes its whole value in its own format: 19:30 (24-hour), 2026-10-26, 2026-10-26T19:30."""
        if (refused := await self._before_act(ctx, _TYPE.journal)) is not None:
            return refused
        bound = await self._guarded(
            ctx, _TYPE.journal, "type", refs=(ref,), confirm=confirm, submit=submit, expect=expect
        )
        if isinstance(bound, str):
            return bound
        fp = bound[ref]
        cmd = {"verb": "type", "ref": ref, "text": text, "submit": submit, "expect": fp}
        return await self._act(ctx, _TYPE.journal, cmd, labels.browser_label(shown(fp), "type"))

    async def browser_fill(
        self,
        ctx: RunContext[Any],
        fields: list[FillField],
        submit: bool = False,
        confirm: bool = False,
        expect: dict[str, dict[str, str]] | None = None,
    ) -> str | ToolReturn[str]:
        """Fill a whole form in one call; submit=true presses Enter afterwards. A date or time
        field takes its whole value in its own format: 19:30 (24-hour), 2026-10-26, 2026-10-26T19:30."""
        if (refused := await self._before_act(ctx, _FILL.journal)) is not None:
            return refused
        if not fields:
            return await self._refuse(_FILL.journal, "no fields", "fill")
        refs = tuple(f.ref for f in fields)
        bound = await self._guarded(
            ctx, _FILL.journal, "fill", refs=refs, confirm=confirm, submit=submit, expect=expect, by_ref=True
        )
        if isinstance(bound, str):
            return bound
        cmd = {
            "verb": "fill",
            "fields": [f.model_dump() for f in fields],
            "submit": submit,
            "expect": bound,
        }
        return await self._act(ctx, _FILL.journal, cmd, labels.browser_label(shown(bound[refs[0]]), "fill"))

    async def browser_select(
        self,
        ctx: RunContext[Any],
        ref: str,
        value: str,
        confirm: bool = False,
        expect: dict[str, str] | None = None,
    ) -> str | ToolReturn[str]:
        """Choose an option in a select by reference."""
        if (refused := await self._before_act(ctx, _SELECT.journal)) is not None:
            return refused
        # Checked like a submit: a select's `change` event may submit the form it sits in.
        bound = await self._guarded(
            ctx,
            _SELECT.journal,
            "select",
            refs=(ref,),
            confirm=confirm,
            submit=True,
            expect=expect,
            choice=value,
        )
        if isinstance(bound, str):
            return bound
        fp = bound[ref]
        cmd = {"verb": "select", "ref": ref, "value": value, "expect": fp}
        return await self._act(ctx, _SELECT.journal, cmd, labels.browser_label(shown(fp), "select"))

    async def browser_press(
        self, ctx: RunContext[Any], key: str, confirm: bool = False, expect: dict[str, str] | None = None
    ) -> str | ToolReturn[str]:
        """Press a key (Enter, Escape, Tab, ArrowDown ...). Enter is checked like a click on the
        form's submit control."""
        if (refused := await self._before_act(ctx, _PRESS.journal)) is not None:
            return refused
        bound = await self._guarded(
            ctx,
            _PRESS.journal,
            f"press {key}",
            refs=("",),
            confirm=confirm,
            submit=(key == "Enter"),
            expect=expect,
        )
        if isinstance(bound, str):
            return bound
        fp = bound[""]
        cmd = {"verb": "press", "key": key, "expect": fp}
        return await self._act(ctx, _PRESS.journal, cmd, labels.browser_label(shown(fp), f"press {key}"))

    async def browser_scroll(
        self, ctx: RunContext[Any], direction: str = "down", ref: str | None = None
    ) -> str | ToolReturn[str]:
        """Scroll the page or an element (up|down). Never asks: scrolling has no consequence to
        approve."""
        # A scroll of the page holds no reference to the page an earlier call left; an element's does.
        check = self._before_act if ref else self._yield_if_held
        if (refused := await check(ctx, _SCROLL.journal)) is not None:
            return refused
        cmd = {"verb": "scroll", "direction": direction, **({"ref": ref} if ref else {})}
        return await self._act(ctx, _SCROLL.journal, cmd, f"scroll {direction}")

    async def browser_screenshot(
        self, ctx: RunContext[Any], save_as: str | None = None
    ) -> str | BinaryContent:
        """A screenshot, for when the tree is not enough (a canvas, a chart, a layout question).
        With `save_as`, a path ending in .png in the work or out folder, it is filed there for your
        deliverable and you get its path back instead of the picture; a computer that cannot save
        from the browser says so."""
        if (refused := await self._yield_if_held(ctx, _SCREENSHOT.journal)) is not None:
            return refused
        if save_as is not None and self.ports.save is None:
            return await self._refuse(_SCREENSHOT.journal, NO_SAVE, "save a screenshot")
        result = await self._call(_SCREENSHOT.journal, {"verb": "screenshot"}, "screenshot")
        if not result.get("ok"):
            return noticed(result, result_text(result))
        png = base64.b64decode(result["png_b64"])
        if save_as is not None:
            try:
                return noticed(result, await _save(self.journal, self.ports, save_as, png))
            except SaveRefused as refused:
                return noticed(result, str(refused))
        if result.get("notice"):
            self.ports.page_reset = True  # an image cannot carry the notice: the next result does
        return BinaryContent(data=png, media_type="image/png")

    async def browser_handoff(self, ctx: RunContext[Any], reason: str) -> str:
        """Hand the browser to a person and wait. Use it at a sign-in you have no credentials for,
        and at anything only a person can do (a captcha, a code sent to their phone, a consent
        only they can give) - never for something your tools can do, and never for a page that
        does not respond (a person would meet the same dead button). `reason` completes the
        sentence "The agent needs someone to ...", e.g. "sign in to admin.example.com". You
        continue on the page they leave: take a new snapshot first."""
        # Someone is already driving: wait for them, as a takeover.
        if (refused := await self._yield_if_held(ctx, _HANDOFF.journal)) is not None:
            return refused
        if self.ports.control is None:
            return HANDOFF_UNAVAILABLE
        # Where the page is, in the sidecar's words: the people asked to sign in never have only
        # the agent's `reason` (page-influenced text) for it. A read, like `guard_action`'s; a page
        # that cannot be read still hands off, naming no host.
        host = await _page_host(self.ports)
        # Last before the park: it holds until the agent next acts. One that fails leaves the fence
        # as it was, and a person can still sign in on a listed site.
        if self.ports.hand_over is not None:
            try:
                await self.ports.hand_over()
            except Exception:
                logfire.warn(
                    "the browser could not hand its window over; the hand-off goes on", _exc_info=True
                )
        raise CallDeferred(metadata=handoff_metadata(reason, host))


def _approver_only(_ctx: RunContext[Any], tool: ToolDefinition) -> ToolDefinition:
    """The acting verb as the model sees it: `expect` is the approver's to set, so it is not offered."""
    schema = tool.parameters_json_schema
    offered = {name: spec for name, spec in schema.get("properties", {}).items() if name != "expect"}
    return dataclasses.replace(tool, parameters_json_schema={**schema, "properties": offered})


def browser_toolset(
    granted: tuple[str, ...], journal: JournalTool | None, ports: BrowserPorts, approve: Approve
) -> FunctionToolset[Any] | None:
    """The browser's verbs over the frame's ports, or None unless the whole family is granted."""
    if any(name not in granted for name in BROWSER_TOOLS):
        return None  # a partial grant is a registry mistake - absent beats broken
    tools = _BrowserTools(journal=journal, ports=ports, approve=approve)

    # Registered one call each, not looped over a tuple - a loop unifies the methods into one
    # union type and pyright (rightly, in strict mode) refuses to match that union against
    # `add_function`'s per-call generic signature.
    #
    # `sequential=True` (every tool inherits it): one page has one cursor, one focus and one
    # epoch, and pydantic-ai otherwise runs a response's calls concurrently - each sidecar
    # round trip is its own exec, so "type, type, click Log in" acted in whatever order the
    # execs finished, and two approved `browser_open`s each REPLACED the proxy list from the
    # same starting set, the last write dropping the other host. As a barrier, a browser call
    # also never overlaps a shell call.
    toolset: FunctionToolset[Any] = FunctionToolset(sequential=True, id="prismoid-browser")
    toolset.add_function(tools.browser_open, name=_OPEN.name)
    toolset.add_function(tools.browser_back, name=_BACK.name)
    toolset.add_function(tools.browser_wait, name=_WAIT.name)
    toolset.add_function(tools.browser_snapshot, name=_SNAPSHOT.name)
    toolset.add_function(tools.browser_click, name=_CLICK.name, prepare=_approver_only)
    toolset.add_function(tools.browser_type, name=_TYPE.name, prepare=_approver_only)
    toolset.add_function(tools.browser_fill, name=_FILL.name, prepare=_approver_only)
    toolset.add_function(tools.browser_select, name=_SELECT.name, prepare=_approver_only)
    toolset.add_function(tools.browser_press, name=_PRESS.name, prepare=_approver_only)
    toolset.add_function(tools.browser_scroll, name=_SCROLL.name)
    toolset.add_function(tools.browser_screenshot, name=_SCREENSHOT.name)
    toolset.add_function(tools.browser_handoff, name=_HANDOFF.name)
    return toolset
