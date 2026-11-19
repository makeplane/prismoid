/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * ScreenPane - the task's Machine, live, in the split slot where artifacts open.
 * Opening the screen closes the reader and opening an artifact closes the screen (route.tsx
 * owns that law, the grid template and the narrow Sheet). The pane owns its socket (`useScreen`,
 * pane rate), its header (the state line and the lease's buttons), the address line, the
 * handoff banner, the action boxes and the input.
 *
 * ## Where the page is
 *   The address line is always there: the page's origin as the server states it (the sidecar
 *   reads its tab's URL), never the agent's words, so whoever takes control to type a password
 *   sees whose page it is. The handoff banner says the same host beside the agent's quoted ask.
 *   A page the browser could not load (a host outside the task's allowed sites, which the egress
 *   proxy refuses) is said in words. Whoever drives always has Back - the page before, or a tab
 *   the page opened closed, back to its opener: a remote browser has no toolbar of its own.
 *
 * ## Who sees what
 *   · everyone who can read the task watches; a private lease masks every socket but the
 *     holder's (the server stops their frames; the last one stays, dimmed)
 *   · a Project editor (a personal task's creator) on a wide screen with a fine pointer gets Take
 *     control, with Private as its second option; narrow and touch screens watch only (driving
 *     needs a keyboard) - except while the agent's hand-off asks for a person: a narrow window
 *     with a fine pointer then drives, or the ask could never be answered there
 *   · the state line follows the task as well as the socket: "agent driving" only while a step
 *     runs, "waiting for you" while a hand-off asks, "the task has finished" once it settled
 *   · the holder gets Hand back, and on a private lease the countdown, with Renew in its last
 *     minute (from 9 minutes into the 10)
 *   · the Project owner (a personal task's creator) gets End {name}'s control
 *
 * ## Input (only while you hold control)
 *   The canvas takes focus. Pointer events map onto the frame's pixels, keys go to the remote
 *   page, a paste goes as `text`, and nothing is ever copied out. Esc twice gives the keyboard
 *   back to this page. The server drops input from anyone else, so this is a courtesy, not the
 *   gate (the relay's host-side gate is).
 *   On a driver whose door serves a control stream (VNC), the holder drives the whole desktop
 *   through noVNC (the relay's `vnc` stream): the frames give way to it while this tab holds
 *   control, and come back when control ends.
 */

import { ArrowLeft, ChevronDown, Globe, X } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type CSSProperties } from "react";
import { DOCK_SHELL } from "~/components/tasks/approval-dock";
import { controlLine, skipHandoff } from "~/components/tasks/control-dock";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import {
  Menu,
  MenuContent,
  MenuFooter,
  MenuItem,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { useScreen, type ScreenCursor, type ScreenStatus } from "~/hooks/use-screen";
import { api, toastError, unwrap } from "~/lib/api/client";
import { TERMINAL, type TaskStatus } from "~/lib/api/events";
import { heldText } from "~/lib/machine-copy";
import { rememberClaim, rememberedClaim } from "~/lib/screen-conn";
import type { GateView, ScreenLease, TaskView } from "~/lib/task-view";
import { cn } from "~/lib/utils";
import type { MachineView } from "./machine-chip";
import { ScreenCanvas } from "./screen-canvas";
import { SCREEN_STATUS_LINE } from "./screen-thumb";
import { ScreenVnc } from "./screen-vnc";
import { useScreenInput } from "./use-screen-input";

/** The screen's share of the split row: fixed (the reader's drag lives in its own pane). */
export const SCREEN_PCT = 55;
/** An action box shows this long after the agent's verb. */
export const ACTION_MS = 1500;
/** Renew appears in a private lease's last minute: 9 minutes into its 10. */
export const RENEW_FROM_S = 60;

export type ScreenBox = { x: number; y: number; w: number; h: number };

const ASLEEP_STATES = new Set(["sleeping", "asleep"]);
/** A waking Machine is on its way back: never "Asleep since" the moment the wake began. */
export const WAKING_LINE = "Waking the Machine";
/** The holder's note when their control ended and nothing names who ended it. */
export const GENERIC_ENDED = "Your control ended.";

export function clockHHMM(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Whole seconds left on a lease, never negative. */
export function secondsLeft(until: string | null, now: number): number {
  return until ? Math.max(0, Math.ceil((Date.parse(until) - now) / 1000)) : 0;
}

/** The address line's words: the page's origin as the server states it, or that there is no web
 *  page (none stated, or the browser shows none). Pure. */
export function addressText(origin: string | null): string {
  return origin || "No web page";
}

/** An origin's host (with its port), for the banner's sentence. Pure. */
function hostOf(origin: string): string {
  try {
    return new URL(origin).host;
  } catch {
    return origin;
  }
}

/** What everyone watching is told when the page could not load a host (the egress proxy refuses
 *  every host outside the task's allowed sites; a host that does not answer looks the same). */
export function unreachableText(origin: string): string {
  return `The browser can't reach ${hostOf(origin)}. It opens only this task's allowed sites.`;
}

/** The claim menu's warning: the mask protects the moment of typing, not the logins after. */
export function claimMenuText(projectName: string | null): string {
  return projectName
    ? `Private hides your screen while you sign in. Afterwards, everyone in ${projectName} can use this browser's logins.`
    : "Private hides your screen while you sign in. Afterwards, this task can use this browser's logins.";
}

/** The header's state line (the strings verbatim). Pure. */
export function paneHeadline(p: {
  status: ScreenStatus;
  lease: ScreenLease | null;
  me: string;
  busy: boolean;
  masked: string | null;
  claiming: boolean;
  asleepSince: string | null;
  /** The lease is mine, but held by another tab (or a reload): this tab has no input. */
  elsewhere?: boolean;
  /** The Machine is waking (UI check D6): the relay may still say asleep. */
  waking?: boolean;
  /** The task's status: only a running task has an agent at the browser. Unknown: running. */
  task?: TaskStatus;
  /** The agent's hand-off asks a person to take the browser. */
  handoff?: boolean;
}): string {
  if (p.status === "off") return "Computer use is off.";
  if (p.status === "closed") return "The screen closed.";
  if (p.status === "no_screen") return SCREEN_STATUS_LINE.no_screen;
  if (p.waking) return WAKING_LINE;
  if (p.asleepSince)
    return `Asleep since ${clockHHMM(p.asleepSince)}. The screen comes back when the next step wakes the Machine.`;
  const holder = p.lease?.holder ?? null;
  const mine = holder != null && holder.id === p.me;
  if (p.claiming || (mine && p.busy)) return "Taking control: the agent is finishing a step";
  if (mine && p.elsewhere) return "You are driving in another tab";
  if (mine) return p.lease?.private ? "You are driving (private)" : "You are driving";
  if (holder) {
    const name = holder.name || "Someone";
    return p.lease?.private ? `${name} is signing in privately` : `${name} is driving`;
  }
  if (p.masked !== null) return `${p.masked || "Someone"} is signing in privately`;
  if (p.status !== "live") return SCREEN_STATUS_LINE[p.status];
  return liveLine(p.task, p.handoff);
}

/** A live screen nobody holds: who is at the browser, by the task's own state. Pure. */
function liveLine(task: TaskStatus | undefined, handoff?: boolean): string {
  if (handoff) return "Live · waiting for you to take control";
  if (task === undefined || task === "running") return "Live · agent driving";
  return TERMINAL.has(task) ? "Live · the task has finished" : "Live · agent not driving";
}

/** The socket states with no picture to watch, so nothing to drive. */
const UNWATCHABLE: ReadonlySet<ScreenStatus> = new Set(["off", "closed", "no_screen"]);

/** The open hand-off the banner shows: nobody holds the browser and there is a picture. Pure. */
function handoffOf(gate: GateView | null, holder: unknown, watchable: boolean): GateView | null {
  return gate?.kind === "control" && gate.source === "handoff" && holder == null && watchable
    ? gate
    : null;
}

/** May this pane drive? An editor with a fine pointer on a wide screen - or on a narrow one while
 *  the agent's hand-off asks a person to take the browser, or the ask could not be answered. Pure. */
export function drivableOf(p: {
  canEdit: boolean;
  coarse: boolean;
  narrow: boolean;
  gate: GateView | null;
}): boolean {
  const asked = p.gate?.kind === "control" && p.gate.source === "handoff";
  return p.canEdit && !p.coarse && (!p.narrow || asked);
}

/** What the holder is told when someone else ended their control, else
 *  null: only an ending the Project owner caused names anyone. Pure. */
export function endedLine(ended: TaskView["controlEnded"], me: string): string | null {
  if (!ended || ended.holderId !== me || !ended.endedBy) return null;
  return `${ended.endedBy.name || "Someone"} ended your control.`;
}

/** The holder's "your control ended" line, one per ending: the record's named line ("Ada ended
 *  your control.") wins over the generic one, and once it shows the generic one never replaces
 *  it (UI check D5: the lease clearing a moment after the record flashed the generic line). Pure. */
export function mergeEndedText(shown: string | null, incoming: string): string {
  return incoming === GENERIC_ENDED && shown !== null ? shown : incoming;
}

/** Is this tab the one that claimed? A claim is this pane's own, with its own connection: the
 *  relay masks EVERY socket but the holder's own (a private lease), so a masked socket is not
 *  the holder's connection (another tab re-claimed), whatever the lease says. Pure. */
export function claimedHereOf(p: {
  claimed: boolean;
  mine: boolean;
  masked: string | null;
}): boolean {
  return p.claimed && p.mine && p.masked === null;
}

/** The claim flag after the server's word on this connection (the relay's `role`) changed: a
 *  re-claim from another tab, a duplicated tab or an ending says it no longer drives, a claim
 *  that landed says it does. An unchanged word is no news - the false it said before this tab's
 *  own claim is not a refusal of it - and no word yet leaves the tab's own record. Pure. */
export function claimedFromRole(
  claimed: boolean,
  role: { now: boolean | null; before: boolean | null },
): boolean {
  return role.now === null || role.now === role.before ? claimed : role.now;
}

/** The claim flag after the lease or the record moved: nobody holding (a hand-back, an end, an
 *  expiry), someone else holding, or a `machine.control_ended` naming me that this pane has not
 *  seen yet, all end it. Pure. */
export function nextClaimed(
  claimed: boolean,
  p: { holderId: string | null; me: string; endedForMe: boolean },
): boolean {
  if (p.holderId === null || p.holderId !== p.me || p.endedForMe) return false;
  return claimed;
}

/** The driver's real cursor over the frame, as percentages: its hot spot at the pointer. Null
 *  without a shape. Pure. */
export function cursorStyle(
  cursor: ScreenCursor,
  size: { w: number; h: number },
): CSSProperties | null {
  const s = cursor.shape;
  if (!s) return null;
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  return {
    left: pct(cursor.x - s.hotX, size.w),
    top: pct(cursor.y - s.hotY, size.h),
    width: pct(s.w, size.w),
    height: pct(s.h, size.h),
  };
}

/** An action box, as percentages of the frame - so it scales with the canvas. Pure. */
export function boxStyle(box: ScreenBox, size: { w: number; h: number }): CSSProperties {
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  return {
    left: pct(box.x, size.w),
    top: pct(box.y, size.h),
    width: pct(box.w, size.w),
    height: pct(box.h, size.h),
  };
}

/** Driving needs a keyboard and a fine pointer: a touch screen watches. */
function useCoarsePointer(): boolean {
  const [coarse, setCoarse] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia("(pointer: coarse)");
    const sync = () => setCoarse(mql.matches);
    sync();
    mql.addEventListener("change", sync);
    return () => mql.removeEventListener("change", sync);
  }, []);
  return coarse;
}

/** A 1 s clock while `active` (the private holder's countdown). */
function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return now;
}

export function ScreenPane({
  taskId,
  machine,
  canEdit,
  isOwner,
  me,
  gate,
  onClose,
  lease,
  action,
  ended = null,
  projectName,
  narrow = false,
  startClaimed = false,
  taskStatus,
}: {
  taskId: string;
  machine: MachineView;
  /** A Project editor of the task, or a personal task's creator (`TaskDetailOut.can_edit`). */
  canEdit: boolean;
  /** May end anyone's control: the Project owner, or a personal task's creator
   *  (`TaskDetailOut.can_end_control`). */
  isOwner: boolean;
  /** The viewer's own id (the shell's user id IS the actor id), to know "you". */
  me: string;
  /** The task's open CONTROL gate, if any (the handoff banner reads it). */
  gate: GateView | null;
  onClose: () => void;
  /** The lease as the page knows it (route.tsx's `currentLease`). */
  lease: ScreenLease | null;
  /** The newest `screen.action` (the reducer's field): a new object per acting verb. */
  action: TaskView["screenAction"];
  /** The newest `machine.control_ended` (the reducer's `controlEnded`): names who ended it. */
  ended?: TaskView["controlEnded"];
  /** Test seam: the pane starts as having claimed control with its own connection. */
  startClaimed?: boolean;
  /** The Project's name for the claim menu's warning; null on a personal task. */
  projectName: string | null;
  /** Rendered in the narrow Sheet: watch only, unless a hand-off asks for a person. */
  narrow?: boolean;
  taskStatus?: TaskStatus; // the state line's (`paneHeadline`)
}) {
  const screen = useScreen(taskId, { pane: true, enabled: true });
  const { send } = screen;
  const coarse = useCoarsePointer();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [pending, setPending] = useState<string | null>(null);
  const privateNoteId = useId();

  const holder = lease?.holder ?? null;
  const mine = holder != null && holder.id === me;
  // Per-tab holding: the lease may be mine from ANOTHER tab (or before a reload). Only a claim
  // made with THIS pane's connection makes input legitimate here; the relay drops the rest.
  // "Take control here" re-claims and rebinds the connection. The connection is kept per task in
  // this tab, and so is the claim (`rememberClaim`): a closed and reopened pane is still the one
  // that claimed, never "another tab".
  const [claimed, setClaimed] = useState(
    () => startClaimed || rememberedClaim(taskId, screen.conn),
  );
  useEffect(() => rememberClaim(taskId, screen.conn, claimed), [taskId, screen.conn, claimed]);
  const endedMine = ended?.holderId === me ? ended.at : null;
  const seenEnded = useRef(endedMine);
  useEffect(() => {
    const endedForMe = endedMine !== null && endedMine !== seenEnded.current;
    seenEnded.current = endedMine;
    setClaimed((c) => nextClaimed(c, { holderId: holder?.id ?? null, me, endedForMe }));
  }, [holder?.id, me, endedMine]);
  const roleBefore = useRef<boolean | null>(null);
  useEffect(() => {
    const role = { now: screen.driver, before: roleBefore.current };
    roleBefore.current = screen.driver;
    setClaimed((c) => claimedFromRole(c, role));
  }, [screen.driver]);
  const claimedHere = claimedHereOf({ claimed, mine, masked: screen.masked });
  const other = holder != null && !mine ? holder : null;
  const waking = machine.state === "waking";
  const asleep = !waking && (screen.status === "asleep" || ASLEEP_STATES.has(machine.state));
  // Nothing to watch is nothing to drive: no Take control, no handoff banner.
  const watchable = !asleep && !waking && !UNWATCHABLE.has(screen.status);
  const drivable = drivableOf({ canEdit, coarse, narrow, gate });
  const dim = screen.masked != null && !mine;
  const now = useNow(mine && lease?.private === true);
  const left = mine && lease?.private ? secondsLeft(lease.until, now) : null;
  const handoff = handoffOf(gate, holder, watchable);

  const headline = paneHeadline({
    status: screen.status,
    lease,
    me,
    busy: screen.busy,
    masked: dim ? screen.masked : null,
    claiming: pending === "claim",
    asleepSince: asleep ? machine.state_since : null,
    elsewhere: mine && !claimedHere,
    waking,
    task: taskStatus,
    handoff: handoff != null,
  });

  async function act(what: string, failure: string, call: () => Promise<unknown>) {
    if (pending) return;
    setPending(what);
    try {
      await call();
    } catch (e) {
      // A held lease answers 409 "{name} is driving the browser." - the toast says it.
      toastError(e, failure);
    } finally {
      setPending(null);
    }
  }
  const params = { path: { task_id: taskId } };
  // Read at click time: a re-claim never downgrades a live private lease from a stale render.
  const leasePrivate = useRef(false);
  leasePrivate.current = lease?.private === true;
  const claim = (priv: boolean) =>
    act("claim", "Couldn't take control.", async () => {
      // A private claim holds input until the mask is seen and settled (createInputHold).
      if (priv) hold.expectPrivate();
      try {
        await unwrap(
          api.POST("/v1/tasks/{task_id}/screen/control", {
            params,
            // the pane's OWN connection: its input is the input that counts
            body: { conn: screen.conn, private: priv },
          }),
        );
      } catch (e) {
        if (priv) hold.cancelPrivate(); // a refused claim holds nothing back
        throw e;
      }
      setClaimed(true);
    });
  // The ending is marked as the holder's own while the call is in flight (the lease may clear
  // on the stream before the reply lands) and unmarked if it failed.
  const handBack = () =>
    act("release", "Couldn't hand back control.", async () => {
      handedBack.current = true;
      try {
        await unwrap(api.DELETE("/v1/tasks/{task_id}/screen/control", { params }));
      } catch (e) {
        handedBack.current = false;
        throw e;
      }
    });
  const renew = () =>
    act("renew", "Couldn't renew your control.", () =>
      unwrap(api.POST("/v1/tasks/{task_id}/screen/control/renew", { params })),
    );
  const endControl = () =>
    act("end", "Couldn't end the control.", () =>
      unwrap(api.DELETE("/v1/tasks/{task_id}/screen/control", { params })),
    );
  const skip = (g: GateView) => act("skip", "Couldn't skip it.", () => skipHandoff(g.gate_id));

  // ── the action box: 1.5 s per acting verb, keyed by the event's `at`, never the one already
  // shown at mount. A timer ends it however the page re-renders (no ref guard a strict-mode
  // double effect could trip, so a box can never stick).
  const [flash, setFlash] = useState<TaskView["screenAction"]>(null);
  const mountedAt = useRef(action?.at ?? null);
  const actionAt = action?.at ?? null;
  useEffect(() => {
    if (!action?.box || actionAt === mountedAt.current) {
      setFlash(null);
      return;
    }
    setFlash(action);
    const t = window.setTimeout(() => setFlash(null), ACTION_MS);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by the event's `at`
  }, [actionAt]);

  // The overlay layer tracks the canvas's box, so boxes land on the frame however it scales.
  const [layer, setLayer] = useState<CSSProperties | null>(null);

  // ── "your control ended": the holder learns it here when it was not their own hand-back ──
  // The record names who ended it ("Ada ended your control."); an expiry names no one. One line
  // and one 5 s timer for both sources, so the named line is never followed by the generic one.
  const [endedText, setEndedText] = useState<string | null>(null);
  const endedTimer = useRef(0);
  const showEnded = useCallback((text: string) => {
    setEndedText((shown) => mergeEndedText(shown, text));
    window.clearTimeout(endedTimer.current);
    endedTimer.current = window.setTimeout(() => setEndedText(null), 5000);
  }, []);
  useEffect(() => () => window.clearTimeout(endedTimer.current), []);
  const wasMine = useRef(mine);
  const handedBack = useRef(false);
  useEffect(() => {
    const lost = wasMine.current && !mine && !handedBack.current;
    wasMine.current = mine;
    handedBack.current = false;
    if (lost) showEnded(GENERIC_ENDED);
  }, [mine, showEnded]);
  const endedMountAt = useRef(ended?.at ?? null);
  const endedAt = ended?.at ?? null;
  useEffect(() => {
    const line = endedAt === endedMountAt.current ? null : endedLine(ended, me);
    if (line) showEnded(line);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by the record's `at`
  }, [endedAt, me]);

  // ── input, only while you hold control, and only where driving is offered at all ──────
  const holding = claimedHere && watchable && drivable;
  const { hold, driving, vncMode, inputProps } = useScreenInput({
    canvasRef,
    send,
    size: screen.size,
    vnc: screen.vnc,
    holding,
    holderId: holder?.id ?? null,
    me,
    leasePrivate: lease?.private === true,
  });
  // The canvas unmounts while the VNC view stands in for it and a new element mounts after: what
  // holds the canvas (the overlay measure, the focus, the wheel listener) re-binds with vncMode.
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () =>
      setLayer({
        left: el.offsetLeft,
        top: el.offsetTop,
        width: el.offsetWidth,
        height: el.offsetHeight,
      });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [vncMode]);

  return (
    <section
      aria-label="The screen"
      className="bg-background relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden border-l"
    >
      <div className="flex h-14 shrink-0 items-center gap-2 border-b px-1.5">
        <IconButton
          variant="ghost"
          size="sm"
          icon={<Icon icon={X} />}
          aria-label="Close the screen"
          onClick={onClose}
        />
        <span
          role="status"
          className="min-w-0 flex-1 truncate text-body-sm-regular font-medium"
          title={headline}
        >
          {headline}
        </span>
        {left != null && (
          <span
            className="text-muted-foreground text-caption-md-regular tabular-nums"
            aria-label="Time left"
          >
            {heldText(left)} left
          </span>
        )}
        {holding && !vncMode && (
          <Button
            size="sm"
            variant="ghost"
            stretch="auto"
            icon={<Icon icon={ArrowLeft} />}
            label="Back"
            aria-label="Back"
            title="Back: the page before, or close a tab the page opened"
            disabled={!driving}
            onClick={() => send({ t: "back" })}
          />
        )}
        {mine && lease?.private && left != null && left <= RENEW_FROM_S && (
          <Button
            size="sm"
            variant="secondary"
            stretch="auto"
            label="Renew"
            disabled={pending != null}
            onClick={() => void renew()}
          />
        )}
        {mine && (
          <Button
            size="sm"
            variant="secondary"
            stretch="auto"
            label="Hand back"
            disabled={pending != null}
            onClick={() => void handBack()}
          />
        )}
        {other && isOwner && (
          <Button
            size="sm"
            variant="danger-outline"
            stretch="auto"
            label={`End ${other.name || "Someone"}'s control`}
            disabled={pending != null}
            onClick={() => void endControl()}
          />
        )}
        {mine && !claimedHere && drivable && watchable && (
          <Button
            size="sm"
            variant="primary"
            stretch="auto"
            label="Take control here"
            disabled={pending != null}
            onClick={() => void claim(leasePrivate.current)}
          />
        )}
        {!mine && drivable && watchable && (
          // Propel takes no className, so the split button's fused edges became a 1px gap.
          <div className="mr-1.5 flex items-center gap-px">
            <Button
              size="sm"
              variant="primary"
              stretch="auto"
              label="Take control"
              disabled={pending != null}
              onClick={() => void claim(false)}
            />
            <Menu>
              <MenuTrigger
                render={
                  <IconButton
                    size="sm"
                    variant="primary"
                    icon={<Icon icon={ChevronDown} />}
                    aria-label="Take control privately"
                    disabled={pending != null}
                  />
                }
              />
              {/* The warning rides the footer: a row's description truncates, and this one must
                  be read whole (the item points at it for screen readers). */}
              <MenuContent
                align="end"
                footer={<MenuFooter id={privateNoteId}>{claimMenuText(projectName)}</MenuFooter>}
              >
                <MenuItem
                  label="Private"
                  aria-describedby={privateNoteId}
                  onClick={() => void claim(true)}
                />
              </MenuContent>
            </Menu>
          </div>
        )}
      </div>

      {/* Never truncated: an ellipsis could hide the very part of a host that says whose it is. */}
      <div
        aria-label="Page address"
        className="text-muted-foreground flex min-h-7 shrink-0 items-center gap-1.5 border-b px-3 py-1 text-caption-md-regular"
      >
        <Globe className="size-3.5 shrink-0" aria-hidden />
        <span className={cn("min-w-0 break-all", screen.origin && "text-foreground font-mono")}>
          {addressText(screen.origin)}
        </span>
      </div>

      <div className="bg-muted/30 relative min-h-0 flex-1">
        {/* the frame, letterboxed by layout alone: the canvas's box IS the drawn frame */}
        <div className="absolute inset-0 grid grid-cols-[minmax(0,1fr)] grid-rows-[minmax(0,1fr)] place-items-center p-3">
          {vncMode && screen.vnc ? (
            <ScreenVnc channel={screen.vnc} className="max-h-full max-w-full" />
          ) : (
            <ScreenCanvas
              canvasRef={canvasRef}
              frame={asleep || waking ? null : screen.frame}
              size={screen.size}
              aria-label={holding ? "The remote browser: you are driving" : "The remote browser"}
              className={cn(
                "max-h-full max-w-full outline-none transition-opacity",
                holding && "focus-visible:ring-ring cursor-default focus-visible:ring-2",
                dim && "opacity-40",
                (asleep || waking || !screen.frame) && "opacity-0",
              )}
              {...inputProps}
            />
          )}
        </div>
        {flash?.box && screen.size && !dim && (
          <div className="pointer-events-none absolute inset-0" style={layer ?? undefined}>
            <div
              className="border-primary absolute rounded-sm border-2"
              style={boxStyle(flash.box, screen.size)}
              aria-hidden
            />
          </div>
        )}
        {!vncMode &&
          !dim &&
          screen.cursor &&
          screen.size &&
          cursorStyle(screen.cursor, screen.size) && (
            <div className="pointer-events-none absolute inset-0" style={layer ?? undefined}>
              <img
                src={screen.cursor.shape!.url}
                alt=""
                aria-hidden
                className="absolute"
                style={cursorStyle(screen.cursor, screen.size) ?? undefined}
              />
            </div>
          )}
        {(dim || asleep || waking || !screen.frame) && !vncMode && (
          <div className="text-muted-foreground pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-body-sm-regular">
            {dim
              ? `${screen.masked || "Someone"} is signing in privately`
              : asleep
                ? "Asleep"
                : waking
                  ? WAKING_LINE
                  : SCREEN_STATUS_LINE[screen.status]}
          </div>
        )}
        {screen.unreachable && !dim && watchable && (
          <div className="absolute inset-x-3 bottom-3 z-10">
            <div className={DOCK_SHELL}>
              <p className="px-1 text-body-sm-regular break-words">
                {unreachableText(screen.unreachable)}
              </p>
            </div>
          </div>
        )}
        {handoff && (
          <div className="absolute inset-x-3 top-3 z-10">
            <div className={cn(DOCK_SHELL, "flex flex-wrap items-center gap-2")}>
              <div className="min-w-0 flex-1 px-1">
                <p className="text-body-sm-regular break-words">{controlLine(handoff)}</p>
                {screen.origin != null && (
                  <p className="text-muted-foreground text-caption-md-regular break-all">
                    {screen.origin ? (
                      <>
                        The browser is on{" "}
                        <span className="text-foreground font-mono">{hostOf(screen.origin)}</span>.
                      </>
                    ) : (
                      "The browser shows no web page."
                    )}
                  </p>
                )}
              </div>
              {canEdit && (
                <Button
                  variant="secondary"
                  size="sm"
                  stretch="auto"
                  label="Skip"
                  disabled={pending != null}
                  onClick={() => void skip(handoff)}
                />
              )}
              {drivable && (
                <Button
                  size="sm"
                  variant="primary"
                  stretch="auto"
                  label="Take control (private)"
                  disabled={pending != null}
                  onClick={() => void claim(true)}
                />
              )}
            </div>
          </div>
        )}
      </div>

      {(holding || endedText) && (
        <p className="text-muted-foreground shrink-0 border-t px-3 py-1.5 text-caption-md-regular">
          {holding && vncMode
            ? "You are driving the desktop. Click it to type into it; click outside it to give the keyboard back to this page."
            : holding
              ? "Press Esc twice to give the keyboard back to this page. A paste is typed into the remote page."
              : endedText}
        </p>
      )}
    </section>
  );
}
