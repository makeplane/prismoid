# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A computer's browser, driven from the worker: the
browser verbs (`BrowserVerbs`) over a CDP connection the driver dials (`cdp_ws.WsCdp` to
the provider's relay), and the step's recording (`Recorder` over the driver's `FrameSink`). It speaks
the sidecar's command protocol, so `FrameDriver.browser` answers the same shapes on every driver:
`ping`, `hold`/`release`, `record`, `finalize`, `shutdown` and the twelve verbs.

What the daemon does for the sidecar (`Daemon.dispatch`) it does here:
* one lock around every verb AND every recording write - the `Recorder`'s guard-then-write steps
  are atomic only under one owner, and a network sink yields;
* a page that moved to another tab is followed before the verb;
* `_record` is popped before the result leaves;
* every exception is a result (a `TimeoutError`, a reply missing a field).

What differs:
* it launches nothing - the computer runs Chromium - so a dropped connection is answered by
  reconnecting at the next verb (epochs and the page's generation continue across it), never by a
  restart;
* `hold` has nothing to pin (no idle stop), and `shutdown` lets go of the session instead of
  stopping a Chromium that is not this process's;
* a person taking the computer's screen - the provider's CDP gate refuses commands `screen_busy`,
  then closes the socket 1013 - is "a person is driving the browser", never a failure the model
  should retry: the CONTROL gate is that person's."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

import logfire

from prismoid.ai import config
from prismoid.ai.frames.cdp_ws import GATE_CLOSE
from prismoid.ai.frames.driver import Owner
from prismoid.ai.frames.frame_sink import FrameSink
from prismoid.ai.frames.image_browser.sidecar.cdp import CdpClosed, CdpConnection, CdpError
from prismoid.ai.frames.image_browser.sidecar.recorder import Recorder
from prismoid.ai.frames.image_browser.sidecar.verbs import (
    SNAPSHOT_CHARS_DEFAULT,
    VERBS,
    BrowserVerbs,
    StaleReference,
)

Connect = Callable[[], Awaitable[CdpConnection]]

Reserve = Callable[[int], Awaitable[int]]
"""Reserve `block` snapshot epochs past the computer's ledger and return the block's base (the old
ceiling). The ledger outlives the step, so a later step's session starts past every epoch an
earlier one could have used."""

EPOCH_BLOCK: Final = 100


def clock_base() -> int:
    """Where a session counts from when no ledger value reaches it: the wall clock
    in milliseconds - 13 digits, past any count a computer's ledger could hold - so it never
    repeats an earlier step's epoch, and serial sessions never collide (a snapshot takes longer
    than a millisecond). The cost: that session's references are long (`e1760000000000.3`)."""
    return time.time_ns() // 1_000_000


RESERVE_TIMEOUT_S: Final = 10.0  # a ledger write that hangs is a failed one, never a stalled verb
RESERVE_TRIES: Final = 10

DRIVING: Final = "a person is driving the browser; take a new snapshot once they let go"
DROPPED: Final = "the browser connection dropped; the next call reconnects"
UNREACHABLE: Final = "the browser could not be reached"
# `BrowserVerbs.handle` words a refused command "browser refused: <message>"; the gate's own message leads.
REFUSED_BUSY: Final = "browser refused: screen_busy"


def _gated(result: dict[str, Any]) -> bool:
    """The provider's CDP gate refused the command: a person holds the screen."""
    return not result.get("ok") and str(result.get("error", "")).startswith(REFUSED_BUSY)


class RemoteBrowser:
    def __init__(
        self,
        connect: Connect,
        recorder: Recorder,
        *,
        snapshot_chars: int = SNAPSHOT_CHARS_DEFAULT,
        reserve: Reserve | None = None,
        block: int = EPOCH_BLOCK,
    ) -> None:
        self._connect = connect
        self.recorder = recorder
        self.snapshot_chars = snapshot_chars
        self._lock = asyncio.Lock()
        self._cdp: CdpConnection | None = None
        self._verbs: BrowserVerbs | None = None
        self._recording = False  # the recorder opened its folder (lazily: the first command that records)
        # Carried across connections within the step, as `Browser.connect` carries them across restarts.
        self._epoch = 0
        self._page_gen = 0
        # Each session's snapshots come from blocks reserved in a ledger that outlives the
        # step, written before the block is used, so a worker that dies mid-step never lets the
        # next session repeat an epoch. None: no ledger (the epochs start at 0, as a sidecar's do).
        self._reserve = reserve
        self._block = block
        self._ceiling: int | None = None  # the highest epoch the ledger has WRITTEN for this session
        self._retry_at = 0  # after a failed write, the epoch at which the ledger is tried again
        self._clocked = False  # the first reservation failed: the session counts from `clock_base`
        self._gated = (
            False  # a person holds the screen: set by a gated verb, cleared by one that reaches the page
        )

    @property
    def connected(self) -> bool:
        return self._verbs is not None

    async def handle(self, cmd: dict[str, Any]) -> dict[str, Any]:
        verb = str(cmd.get("verb", ""))
        if verb == "ping":
            return {"ok": True, "chromium": "running" if self.connected else "stopped", "held": False}
        if verb in ("hold", "release"):
            # The daemon's idle stop has no twin here: a computer's Chromium runs on, so a hold has
            # nothing to pin. Answered as the daemon answers it.
            return {"ok": True, "held": verb == "hold"}
        async with self._lock:
            try:
                return await self._locked(verb, cmd)
            except Exception as e:  # every error is a result, as `Daemon.dispatch` makes it
                return {"ok": False, "error": type(e).__name__}

    async def _locked(self, verb: str, cmd: dict[str, Any]) -> dict[str, Any]:
        if verb == "record":
            try:
                self._recording = await self.recorder.open(str(cmd.get("dir") or self.recorder.folder))
            except Exception as e:
                # The folder `record` named stays the one to retry; nothing records meanwhile, so no
                # row can overwrite the trail the failed open could not read.
                self._recording = False
                await self._close_sink(e)
                return {"ok": False, "error": "recording folder unavailable"}
            return {"ok": True} if self._recording else {"ok": False, "error": "recording folder unavailable"}
        if verb == "finalize":
            await self._record_ready()
            return await self.recorder.finalize()
        if verb == "shutdown":
            # Before a sleep the daemon stops Chromium so it flushes its cookies. A computer's
            # Chromium is not this process's to stop: the session lets go and the page stays.
            await self._drop()
            return {"ok": True}
        if verb not in VERBS:
            return {"ok": False, "error": f"unknown verb {verb!r}"}
        return await self._verb(verb, cmd)

    async def _record_ready(self) -> None:
        """Recording is evidence, never authority: a file door that hiccups records NOTHING for now
        (the sink is closed, so no row can overwrite a live trail) and the next verb retries."""
        if self._recording:
            return
        try:
            self._recording = await self.recorder.open()
        except Exception as e:
            await self._close_sink(e)

    async def _close_sink(self, e: Exception) -> None:
        logfire.warn("the recording could not open: {err}", err=type(e).__name__)
        with contextlib.suppress(Exception):
            await self.recorder.sink.open("")  # a sink refuses "": ready is False until the next open

    async def _reserved(self) -> None:
        """Make sure every epoch the next verb can use is under a ceiling the ledger has WRITTEN
        (a verb with `snapshot: true` takes two, so headroom is two). The local ceiling only ever
        mirrors a successful reserve (`base + block`); when that is still short of the session's
        epoch (it counted on after a failure) the reserve repeats, each block written before use.
        Best-effort: a ledger that cannot be written in time is logged and the session counts on (a
        collision needs a crash and a lost write together); it never stops the browser. When the
        session's FIRST reservation fails no ledger value is known, so it counts from `clock_base`
        instead of 0 and asks the ledger no more: no ledger count reaches it."""
        if self._reserve is None or self._clocked:
            return
        for _ in range(RESERVE_TRIES):
            current = self._verbs.epoch if self._verbs is not None else self._epoch
            need = current + 2
            if self._ceiling is not None and need <= self._ceiling:
                return
            if current < self._retry_at:  # a failed write is not retried on every verb
                return
            try:
                base = await asyncio.wait_for(self._reserve(self._block), RESERVE_TIMEOUT_S)
            except Exception as e:
                if self._ceiling is None and self._verbs is None:
                    self._clocked = True
                    self._epoch = max(self._epoch, clock_base())
                    logfire.warn(
                        "the browser's epoch ledger could not be reserved: {err}; this session counts "
                        "from the clock, past it ({base})",
                        err=type(e).__name__,
                        base=self._epoch,
                    )
                    return
                logfire.warn(
                    "the browser's epoch ledger could not be written: {err}; epochs {lo}..{hi} are not covered",
                    err=type(e).__name__,
                    lo=(self._ceiling or 0) + 1,
                    hi=need,
                )
                self._retry_at = current + self._block
                return
            self._retry_at = 0
            if self._verbs is None:
                self._epoch = max(self._epoch, base)
            elif base > (self._ceiling if self._ceiling is not None else current):
                logfire.warn(
                    "the epoch ledger is past a live session: ledger base {base}, session epoch {epoch}",
                    base=base,
                    epoch=current,
                )
            self._ceiling = base + self._block
        logfire.warn(
            "the browser's epoch ledger stayed behind the session after {n} reservations", n=RESERVE_TRIES
        )

    async def _verb(self, verb: str, cmd: dict[str, Any]) -> dict[str, Any]:
        await self._record_ready()
        try:
            verbs = await self._session()
        except Exception as e:  # a ticket refused, a relay down, no page to attach to
            busy = isinstance(e, CdpError) and e.message.startswith("screen_busy")
            if busy:
                self._gated = True
            logfire.warn("the browser session could not open: {err}", err=type(e).__name__)
            return {"ok": False, "error": DRIVING if self._gated else UNREACHABLE}
        try:
            await self._reserved()
            if await verbs.follow():  # a tab the page opened: this verb acts there
                self.recorder.forget_targets()
            result = await verbs.handle(cmd)
        except StaleReference as s:
            return {"ok": False, "error": f"stale reference {s.ref}; take a new snapshot"}
        except TimeoutError:
            # A connection open on our side but silently dead: drop it so the next verb redials.
            await self._drop()
            return {"ok": False, "error": "TimeoutError"}
        except (CdpClosed, ConnectionError):
            driving = getattr(self._cdp, "close_code", None) == GATE_CLOSE
            self._gated = self._gated or driving
            await self._drop()
            return {"ok": False, "error": DRIVING if driving else DROPPED}
        if _gated(result):
            self._gated = True
            return {"ok": False, "error": DRIVING}
        self._gated = False
        record = result.pop("_record", None)
        await self.recorder.file(verb, cmd, result, record)
        if (targets := result.pop("_targets", None)) is not None:
            self.recorder.remember_targets(targets)
        return result

    async def _session(self) -> BrowserVerbs:
        if self._verbs is not None:
            return self._verbs
        await self._reserved()
        cdp = await self._connect()
        try:
            verbs = BrowserVerbs(
                cdp, snapshot_chars=self.snapshot_chars, epoch=self._epoch, page_gen=self._page_gen
            )
            await verbs.attach()
        except BaseException:
            with contextlib.suppress(Exception):
                await cdp.close()
            if getattr(cdp, "close_code", None) == GATE_CLOSE:  # a person took the screen mid-open
                self._gated = True
            raise
        self.recorder.forget_targets()  # a new session's page: no snapshot's names apply
        self._cdp, self._verbs = cdp, verbs
        return verbs

    async def _drop(self) -> None:
        cdp, verbs = self._cdp, self._verbs
        self._cdp = self._verbs = None
        if verbs is not None:
            self._epoch, self._page_gen = verbs.epoch, verbs.page_gen
            verbs.drop_tabs()
        if cdp is not None:
            with contextlib.suppress(Exception):
                await cdp.close()

    async def drop(self) -> None:
        """Close the connection; the next verb reconnects (a verb cut off by its timeout)."""
        async with self._lock:
            await self._drop()

    async def close(self) -> None:
        """The step let go of the Machine (`FrameDriver.close`): the session ends, the page stays."""
        await self.drop()


# ── a driver's browser, driven from the worker ──
# A remote driver and the device both drive their browser from this process: the same command
# deadline, the same bounded drop and close, the same recording through the driver's own file
# door, and the same snapshot-epoch ledger. Each driver keeps only how it dials (`Connect`) and
# hands in its module's `logfire` and close bound at call time, so its logs stay its own.


class Log(Protocol):
    def warn(self, msg_template: str, /, **attributes: Any) -> None: ...
    def exception(self, msg_template: str, /, **attributes: Any) -> None: ...


class BrowserFrame(Protocol):
    """What a driver hands in: its file door (the recording and the ledger ride it) and where its
    step's recording lives."""

    @property
    def recording_dir(self) -> str: ...
    async def read_bytes(self, path: str) -> bytes: ...
    async def write_bytes(
        self, path: str, data: bytes, *, mode: int = 0o644, owner: Owner = "agent"
    ) -> None: ...


class Closes(Protocol):
    async def close(self) -> None: ...


def remote_for(frame: BrowserFrame, dial: Connect, reserve: Reserve) -> RemoteBrowser:
    """The step's browser session: `dial` for the CDP connection, the recording in
    `frame.recording_dir` through the frame's file door, epochs reserved by `reserve`."""
    return RemoteBrowser(
        dial,
        Recorder(FrameSink(frame), frame.recording_dir),
        snapshot_chars=config.current().worker.browser_snapshot_chars,
        reserve=reserve,
    )


async def run_command(
    remote: RemoteBrowser,
    command: dict[str, Any],
    timeout: float | None,  # noqa: ASYNC109 - the verb's deadline, as `FrameDriver.browser` takes it
    *,
    bound: float,
    log: Log,
    label: str,
    ident: str,
) -> dict[str, Any]:
    """One command (`FrameDriver.browser`): every error is a result, and a verb past `timeout`
    (default PRISMOID_COMPUTER_USE_COMMAND_TIMEOUT_S) is cut, said, and its session dropped within
    `bound`, so the next one reconnects."""
    timeout = config.current().computer_use.command_timeout_s if timeout is None else timeout
    try:
        return await asyncio.wait_for(remote.handle(command), timeout=timeout)
    except TimeoutError:
        try:
            await asyncio.wait_for(remote.drop(), bound)
        except TimeoutError:
            log.warn(f"{label} browser drop timed out for {{id}}", id=ident)
        except Exception:
            log.exception(f"{label} browser drop failed for {{id}}", id=ident)
        return {"ok": False, "error": f"browser timed out after {timeout}s"}


async def close_session(browser: Closes | None, *, bound: float, log: Log, label: str, ident: str) -> None:
    """The step let go (`FrameDriver.close`): its session ends within `bound`; the browser runs on.
    Never raises."""
    if browser is None:
        return
    try:
        await asyncio.wait_for(browser.close(), bound)
    except TimeoutError:
        log.warn(f"{label} browser close timed out for {{id}}", id=ident)
    except Exception:
        log.exception(f"{label} browser close failed for {{id}}", id=ident)


async def reserve_epochs(frame: BrowserFrame, ledger: str, block: int, *, log: Log, ident: str) -> int:
    """Reserve `block` snapshot epochs in the frame's ledger at `ledger` and return the block's
    base. Written before any epoch of the block is used. A missing ledger is base 0 (a fresh
    computer's first snapshot is epoch 1). A ledger that is there but corrupt (empty, not UTF-8,
    not an integer, negative) is never read as 0 - that would repeat an earlier step's epochs - and
    never fails every reservation forever: it is healed past the millisecond clock (`clock_base`,
    past any count a ledger could hold), logged. The ledger is the system's, 0444."""
    try:
        raw = await frame.read_bytes(ledger)
    except FileNotFoundError:
        base = 0  # no ledger yet: the first session of this computer
    else:
        try:
            base = int(raw.decode().strip())
        except ValueError:  # UnicodeDecodeError is one; so are empty and non-integer
            base = -1
        if base < 0:
            base = clock_base()
            log.warn(
                "the browser epoch ledger of {id} was corrupt; healed past the clock ({base})",
                id=ident,
                base=base,
            )
    await frame.write_bytes(ledger, str(base + block).encode(), mode=0o444, owner="system")
    return base
