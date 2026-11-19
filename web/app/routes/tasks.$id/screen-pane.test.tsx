/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The screen pane's states: who drives, the mask, the countdown and Renew,
// the handoff banner, "Taking control", asleep, and who gets which button. The socket is the
// hook's (hooks/use-screen.test.ts); here it is a fixed snapshot, and the page is static.
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ScreenSnapshot } from "~/hooks/use-screen";
import { createVncChannel } from "~/lib/screen-vnc";
import type { GateView, ScreenLease } from "~/lib/task-view";

vi.mock("./screen-vnc", () => ({ ScreenVnc: () => <div data-vnc="mounted" /> }));
const screen = vi.hoisted(() => ({ value: {} as Partial<ScreenSnapshot> }));
vi.mock("~/hooks/use-screen", () => ({
  useScreen: () => ({
    frame: { width: 1280, height: 900, close: () => {} },
    size: { w: 1280, h: 900 },
    status: "live",
    masked: null,
    busy: false,
    origin: null,
    unreachable: null,
    driver: null,
    vnc: null,
    cursor: null,
    conn: "tpane00001",
    send: () => {},
    ...screen.value,
  }),
}));

import {
  addressText,
  boxStyle,
  claimMenuText,
  claimedHereOf,
  claimedFromRole,
  cursorStyle,
  endedLine,
  GENERIC_ENDED,
  mergeEndedText,
  nextClaimed,
  paneHeadline,
  ScreenPane,
  secondsLeft,
  unreachableText,
} from "./screen-pane";
import { vncModeOf } from "./use-screen-input";

const T0 = Date.parse("2026-10-02T12:00:00Z");
const at = (s: number) => new Date(T0 + s * 1000).toISOString();
const ADA = { id: "ada", name: "Ada" };
const BOB = { id: "bob", name: "Bob" };
const MACHINE = {
  state: "idle",
  profile: "browser",
  state_since: "2026-10-02T11:58:00Z",
  last_active_at: "2026-10-02T11:58:00Z",
};
const HANDOFF: GateView = {
  gate_id: "g1",
  kind: "control",
  source: "handoff",
  title: 'The agent needs someone to "sign in to admin.acme.io"',
  reason: "sign in to admin.acme.io",
  questions: [],
};

function pane(over: Partial<Parameters<typeof ScreenPane>[0]> = {}) {
  return renderToStaticMarkup(
    <ScreenPane
      taskId="t1"
      machine={MACHINE}
      canEdit
      isOwner={false}
      me="ada"
      gate={null}
      onClose={() => {}}
      lease={null}
      action={null}
      projectName="Launch"
      {...over}
    />,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  screen.value = {};
});
afterEach(() => vi.useRealTimers());

describe("the agent drives", () => {
  it("says so; an editor may take control, a viewer gets no buttons", () => {
    const editor = pane();
    expect(editor).toContain("Live · agent driving");
    expect(editor).toContain(">Take control<");
    const viewer = pane({ canEdit: false });
    expect(viewer).toContain("Live · agent driving");
    expect(viewer).not.toContain("Take control");
    expect(viewer).not.toContain("Hand back");
  });

  it("narrow and touch screens watch only", () => {
    expect(pane({ narrow: true })).not.toContain("Take control");
  });
});

describe("the state line follows the task", () => {
  it("a settled task never says the agent is driving", () => {
    for (const taskStatus of ["delivered", "failed", "cancelled"] as const) {
      const html = pane({ taskStatus });
      expect(html).toContain("Live · the task has finished");
      expect(html).not.toContain("agent driving");
    }
  });

  it("a task between steps says the agent is not driving; a running one that it is", () => {
    expect(pane({ taskStatus: "waiting" })).toContain("Live · agent not driving");
    expect(pane({ taskStatus: "running" })).toContain("Live · agent driving");
  });

  it("an open hand-off says it waits for you", () => {
    expect(pane({ taskStatus: "waiting", gate: HANDOFF })).toContain(
      "Live · waiting for you to take control",
    );
  });

  it("a hand-off is answerable on a narrow window with a fine pointer", () => {
    expect(pane({ narrow: true, gate: HANDOFF })).toContain(">Take control<");
    expect(pane({ narrow: true, gate: HANDOFF, canEdit: false })).not.toContain("Take control");
  });
});

describe("someone else drives", () => {
  it("names them; the Project owner may end it, an editor still may ask (the 409 names them)", () => {
    const lease: ScreenLease = { holder: BOB, private: false, until: at(60) };
    const owner = pane({ lease, isOwner: true });
    expect(owner).toContain("Bob is driving");
    expect(owner).toContain("End Bob&#x27;s control");
    expect(owner).toContain(">Take control<");
    const editor = pane({ lease });
    expect(editor).not.toContain("End Bob");
  });

  it("a private lease masks: the last frame dimmed, and the holder named", () => {
    screen.value = { masked: "Bob" };
    const html = pane({ lease: { holder: BOB, private: true, until: at(600) } });
    expect(html).toContain("Bob is signing in privately");
    expect(html).toContain("opacity-40");
  });
});

describe("you drive", () => {
  it("non-private: Hand back, no countdown, and the Esc-Esc hint", () => {
    const html = pane({
      lease: { holder: ADA, private: false, until: at(60) },
      startClaimed: true,
    });
    expect(html).toContain("You are driving");
    expect(html).not.toContain("(private)");
    expect(html).toContain(">Hand back<");
    expect(html).not.toContain(">Take control<");
    expect(html).not.toContain("Time left");
    expect(html).toContain("Press Esc twice");
    expect(html).toContain('tabindex="0"'); // the canvas takes focus
  });

  it("private: the countdown, and Renew only in the last minute", () => {
    const early = pane({
      lease: { holder: ADA, private: true, until: at(540) },
      startClaimed: true,
    });
    expect(early).toContain("You are driving (private)");
    expect(early).toContain("9m left");
    expect(early).not.toContain(">Renew<");
    const late = pane({ lease: { holder: ADA, private: true, until: at(45) }, startClaimed: true });
    expect(late).toContain("45s left");
    expect(late).toContain(">Renew<");
  });

  it("while the agent's verb finishes: Taking control", () => {
    screen.value = { busy: true };
    expect(
      pane({ lease: { holder: ADA, private: false, until: at(60) }, startClaimed: true }),
    ).toContain("Taking control: the agent is finishing a step");
  });

  it("the canvas takes no input when you do not hold control", () => {
    expect(pane()).toContain('tabindex="-1"');
  });

  it("the lease is mine but another tab claimed it: no input here, and Take control here", () => {
    const html = pane({ lease: { holder: ADA, private: true, until: at(500) } });
    expect(html).toContain("You are driving in another tab");
    expect(html).toContain(">Take control here<");
    expect(html).toContain(">Hand back<");
    expect(html).toContain('tabindex="-1"');
    expect(html).not.toContain("Press Esc twice");
    // narrow screens watch only: no re-claim either
    expect(
      pane({ lease: { holder: ADA, private: false, until: at(60) }, narrow: true }),
    ).not.toContain("Take control here");
  });

  it("a narrow or touch screen never drives, even holding the lease from this tab", () => {
    const html = pane({
      lease: { holder: ADA, private: false, until: at(60) },
      startClaimed: true,
      narrow: true,
    });
    expect(html).not.toContain("Press Esc twice");
    expect(html).toContain('tabindex="-1"');
  });

  it("whether this tab drives is the server's word once it speaks (claimedFromRole)", () => {
    // a re-claim from another tab, or a duplicated tab: the server says this connection does not
    expect(claimedFromRole(true, { now: false, before: true })).toBe(false);
    expect(claimedFromRole(false, { now: true, before: false })).toBe(true);
    // unchanged (a stale word from before this tab's own claim) or unknown: the tab's own record
    expect(claimedFromRole(true, { now: false, before: false })).toBe(true);
    expect(claimedFromRole(true, { now: null, before: true })).toBe(true);
  });

  it("an empty holder name reads Someone", () => {
    const html = pane({
      lease: { holder: { id: "bob", name: "" }, private: false, until: at(60) },
      isOwner: true,
    });
    expect(html).toContain("Someone is driving");
    expect(html).toContain("End Someone&#x27;s control");
  });
});

describe("the handoff banner", () => {
  it("an editor: the gate's line, Take control (private) and Skip", () => {
    const html = pane({ gate: HANDOFF });
    expect(html).toContain("The agent needs someone to &quot;sign in to admin.acme.io&quot;");
    expect(html).toContain(">Take control (private)<");
    expect(html).toContain(">Skip<");
  });

  it("a viewer sees the banner with no buttons", () => {
    const html = pane({ gate: HANDOFF, canEdit: false });
    expect(html).toContain("The agent needs someone to &quot;sign in to admin.acme.io&quot;");
    expect(html).not.toContain(">Skip<");
    expect(html).not.toContain("Take control (private)");
  });

  it("says where the page is in the server's words, beside the agent's own", () => {
    screen.value = { origin: "https://accounts.example.net:8443" };
    const html = pane({ gate: HANDOFF });
    expect(html).toContain("The browser is on <span");
    expect(html).toContain("accounts.example.net:8443</span>");
    screen.value = { origin: "" };
    expect(pane({ gate: HANDOFF })).toContain("The browser shows no web page.");
    screen.value = { origin: null }; // not stated yet: the banner claims nothing
    expect(pane({ gate: HANDOFF })).not.toContain("The browser is on");
  });

  it("goes away while someone drives, and a takeover gate never shows it", () => {
    expect(
      pane({ gate: HANDOFF, lease: { holder: BOB, private: true, until: at(600) } }),
    ).not.toContain(">Skip<");
    expect(pane({ gate: { ...HANDOFF, source: "takeover" } })).not.toContain(">Skip<");
  });
});

describe("the address line", () => {
  it("states the page's origin as the server does, for watchers and drivers alike", () => {
    screen.value = { origin: "https://accounts.example.com" };
    for (const html of [
      pane(),
      pane({ canEdit: false }),
      pane({ lease: { holder: ADA, private: true, until: at(600) }, startClaimed: true }),
    ]) {
      expect(html).toContain('aria-label="Page address"');
      expect(html).toContain(">https://accounts.example.com<");
    }
  });

  it("is there with no page too, and never empty", () => {
    screen.value = { origin: "", status: "stopped", frame: null };
    expect(pane()).toContain(">No web page<");
    screen.value = { origin: null, status: "asleep", frame: null };
    expect(pane({ machine: { ...MACHINE, state: "asleep" } })).toContain(
      'aria-label="Page address"',
    );
    expect(addressText(null)).toBe("No web page");
    expect(addressText("http://127.0.0.1:8088")).toBe("http://127.0.0.1:8088");
  });
});

describe("a page the browser cannot reach, and the way back", () => {
  it("says so to everyone watching", () => {
    screen.value = { origin: "https://off.example", unreachable: "https://off.example:8443" };
    const watcher = pane({ canEdit: false });
    expect(watcher).toContain(
      "reach off.example:8443. It opens only this task&#x27;s allowed sites.",
    );
    expect(watcher).not.toContain('aria-label="Back"');
    expect(unreachableText("https://off.example:8443")).toBe(
      "The browser can't reach off.example:8443. It opens only this task's allowed sites.",
    );
  });

  // A remote browser has no toolbar, and a page an http link was refused on is no error page.
  it("whoever drives always has Back: the page before, or a tab the page opened closed", () => {
    const holder = pane({
      lease: { holder: ADA, private: false, until: at(60) },
      startClaimed: true,
    });
    expect(holder).toContain('aria-label="Back"');
    expect(pane({ lease: { holder: BOB, private: false, until: at(60) } })).not.toContain(
      'aria-label="Back"',
    );
  });

  it("says nothing while the page loaded, or before anything is stated", () => {
    for (const unreachable of ["", null]) {
      screen.value = { unreachable };
      expect(
        pane({ lease: { holder: ADA, private: false, until: at(60) }, startClaimed: true }),
      ).not.toContain("reach");
    }
  });
});

describe("no picture", () => {
  it("asleep: the time it slept, and no way to drive", () => {
    const html = pane({ machine: { ...MACHINE, state: "asleep" } });
    expect(html).toContain("Asleep since ");
    expect(html).toContain("The screen comes back when the next step wakes the Machine.");
    expect(html).not.toContain(">Take control<");
  });

  it("off and starting say so", () => {
    screen.value = { status: "off", frame: null };
    expect(pane()).toContain("Computer use is off.");
    screen.value = { status: "starting", frame: null };
    expect(pane()).toContain("Starting the browser");
  });

  it("a stopped browser says when it starts again (UI check O7)", () => {
    screen.value = { status: "stopped", frame: null };
    expect(pane()).toContain("The browser is stopped. It starts with the agent&#x27;s next step.");
  });

  it("a Machine with no screen says so, and nobody is offered control (ADR-065)", () => {
    // Found on #12+#13: a sandbar Machine read "The browser is stopped. It starts with the agent's
    // next step." while the agent browsed, and Take control "worked" with nothing to drive.
    screen.value = { status: "no_screen", frame: null };
    const html = pane();
    expect(html).toContain("This Machine has no live screen yet.");
    expect(html).not.toContain("The browser is stopped");
    expect(html).not.toContain("Take control");
    expect(pane({ gate: HANDOFF })).not.toContain("Take control");
  });

  it("a waking Machine says so, never 'Asleep since' the moment it began to wake (UI check D6)", () => {
    screen.value = { status: "asleep", frame: null };
    const html = pane({
      machine: { ...MACHINE, state: "waking", state_since: "2026-10-02T12:15:00Z" },
    });
    expect(html).toContain("Waking the Machine");
    expect(html).not.toContain("Asleep since");
    expect(html).not.toContain(">Take control<");
  });
});

describe("the pure pieces", () => {
  it("paneHeadline: a mask with no lease known still names the holder", () => {
    expect(
      paneHeadline({
        status: "live",
        lease: null,
        me: "ada",
        busy: false,
        masked: "Bob",
        claiming: false,
        asleepSince: null,
      }),
    ).toBe("Bob is signing in privately");
    expect(
      paneHeadline({
        status: "live",
        lease: null,
        me: "ada",
        busy: false,
        masked: null,
        claiming: true,
        asleepSince: null,
      }),
    ).toBe("Taking control: the agent is finishing a step");
  });

  it("the countdown counts whole seconds down to zero", () => {
    expect(secondsLeft(at(61), T0)).toBe(61);
    expect(secondsLeft(at(-5), T0)).toBe(0);
    expect(secondsLeft(null, T0)).toBe(0);
  });

  it("the claim menu warns the Project, by name", () => {
    expect(claimMenuText("Launch")).toBe(
      "Private hides your screen while you sign in. Afterwards, everyone in Launch can use this browser's logins.",
    );
  });

  it("the holder is told who ended their control", () => {
    const ended = { holderId: "ada", endedBy: BOB, reason: "owner", at: at(0) };
    expect(endedLine(ended, "ada")).toBe("Bob ended your control.");
    expect(endedLine({ ...ended, endedBy: { id: "bob", name: "" } }, "ada")).toBe(
      "Someone ended your control.",
    );
    expect(endedLine({ ...ended, endedBy: null }, "ada")).toBeNull(); // an expiry names no one
    expect(endedLine(ended, "bob")).toBeNull(); // not about me
    expect(endedLine(null, "ada")).toBeNull();
  });

  it("the named ending is never followed by the generic one (UI check D5)", () => {
    const named = "Ada ended your control.";
    expect(mergeEndedText(null, GENERIC_ENDED)).toBe(GENERIC_ENDED);
    expect(mergeEndedText(null, named)).toBe(named);
    expect(mergeEndedText(GENERIC_ENDED, named)).toBe(named); // the record names who, after the lease cleared
    expect(mergeEndedText(named, GENERIC_ENDED)).toBe(named); // the lease clears after the record
  });

  it("a masked socket is not the holder's: another tab re-claimed privately", () => {
    expect(claimedHereOf({ claimed: true, mine: true, masked: null })).toBe(true);
    expect(claimedHereOf({ claimed: true, mine: true, masked: "Ada" })).toBe(false);
    expect(claimedHereOf({ claimed: true, mine: true, masked: "" })).toBe(false);
    expect(claimedHereOf({ claimed: true, mine: false, masked: null })).toBe(false);
    expect(claimedHereOf({ claimed: false, mine: true, masked: null })).toBe(false);
  });

  it("the claim resets when the lease clears, moves, or the record says my control ended", () => {
    const base = { holderId: "ada", me: "ada", endedForMe: false };
    expect(nextClaimed(true, base)).toBe(true);
    expect(nextClaimed(true, { ...base, holderId: null })).toBe(false); // handed back in tab 1, tab 2 claims next
    expect(nextClaimed(true, { ...base, holderId: "bob" })).toBe(false);
    expect(nextClaimed(true, { ...base, endedForMe: true })).toBe(false);
    expect(nextClaimed(false, base)).toBe(false); // never a claim from thin air
  });

  it("the headline says another tab", () => {
    expect(
      paneHeadline({
        status: "live",
        lease: { holder: ADA, private: false, until: null },
        me: "ada",
        busy: false,
        masked: null,
        claiming: false,
        asleepSince: null,
        elsewhere: true,
      }),
    ).toBe("You are driving in another tab");
  });

  it("an action box scales with the frame", () => {
    expect(boxStyle({ x: 640, y: 450, w: 128, h: 90 }, { w: 1280, h: 900 })).toEqual({
      left: "50%",
      top: "50%",
      width: "10%",
      height: "10%",
    });
  });
});

describe("a control-stream driver: the desktop's VNC while holding control", () => {
  const MINE: ScreenLease = { holder: ADA, private: false, until: at(55) };

  it("mounts noVNC when this tab holds control and the relay streams VNC", () => {
    screen.value = { vnc: createVncChannel(() => {}), driver: true };
    const html = pane({ lease: MINE, startClaimed: true });
    expect(html).toContain('data-vnc="mounted"');
    expect(html).not.toContain("<canvas");
  });

  it("returns to the frames when control ends: the lease released, the VNC still closing", () => {
    screen.value = { vnc: createVncChannel(() => {}), driver: false };
    const html = pane({ lease: null, startClaimed: true });
    expect(html).not.toContain('data-vnc="mounted"');
    expect(html).toContain("<canvas");
  });

  it("a Docker Machine never mounts it: no VNC stream, the frames and today's input", () => {
    screen.value = { vnc: null, driver: true };
    const html = pane({ lease: MINE, startClaimed: true });
    expect(html).not.toContain('data-vnc="mounted"');
    expect(html).toContain("<canvas");
  });

  it("someone watching never mounts it", () => {
    screen.value = { vnc: createVncChannel(() => {}) };
    const html = pane({ me: "bob", lease: MINE });
    expect(html).not.toContain('data-vnc="mounted"');
  });

  it("vncModeOf is holding and a stream", () => {
    const ch = createVncChannel(() => {});
    expect(vncModeOf({ holding: true, vnc: ch })).toBe(true);
    expect(vncModeOf({ holding: false, vnc: ch })).toBe(false);
    expect(vncModeOf({ holding: true, vnc: null })).toBe(false);
  });

  it("the cursor lands at its hot spot, in frame percentages", () => {
    const cursor = {
      x: 640,
      y: 400,
      shape: { url: "blob:x", w: 32, h: 32, hotX: 0, hotY: 0 },
    };
    expect(cursorStyle(cursor, { w: 1280, h: 800 })).toEqual({
      left: "50%",
      top: "50%",
      width: "2.5%",
      height: "4%",
    });
    expect(cursorStyle({ ...cursor, shape: null }, { w: 1280, h: 800 })).toBeNull();
  });
});
