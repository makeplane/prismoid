/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The rail's Machine section carries the screen's thumbnail only while a browser Machine is warm
//: asleep it disappears, and a base Machine never had a screen.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("~/hooks/use-screen", () => ({
  useScreen: () => ({
    frame: null,
    size: null,
    status: "live",
    masked: null,
    busy: false,
    conn: "tthumb0001",
    send: () => {},
  }),
}));

import type { MachineView } from "./machine-chip";
import { TaskRail } from "./task-rail";

const machine = (state: string, profile = "browser") => ({
  state,
  profile,
  state_since: "2026-10-02T12:00:00Z",
  last_active_at: "2026-10-02T12:00:00Z",
});

// `null` renders the rail without a task id (a default parameter would swallow `undefined`).
const rail = (m: MachineView, taskId: string | null = "t1") =>
  renderToStaticMarkup(
    <TaskRail
      steps={[]}
      artifacts={[]}
      machine={m}
      taskId={taskId ?? undefined}
      screenHolder="Ada"
      onOpenScreen={() => {}}
      onOpenArtifact={() => {}}
    />,
  );

describe("the rail's screen thumbnail", () => {
  it("shows on a warm browser Machine: running, parked, idle - with the holder", () => {
    for (const state of ["running", "parked", "idle"]) {
      const html = rail(machine(state));
      expect(html).toContain("Open the screen");
      expect(html).toContain("Ada is driving");
    }
  });

  it("disappears asleep, and a base Machine never has one", () => {
    for (const state of ["waking", "sleeping", "asleep", "gone"])
      expect(rail(machine(state))).not.toContain("Open the screen");
    expect(rail(machine("running", "base"))).not.toContain("Open the screen");
  });

  it("needs the task id", () => {
    expect(rail(machine("running"), null)).not.toContain("Open the screen");
  });
});

describe("TaskMachine", () => {
  // Where a cloud Machine shows its screen, a device Machine says
  // where its browser is - and never opens a screen socket for it.
  const device = (device_name: string | null) => ({
    ...machine("running"),
    driver: "device" as const,
    device_name,
  });

  it("shows Browser on MacBook Pro", () => {
    const html = rail(device("MacBook Pro"));
    expect(html).toContain("Browser on MacBook Pro");
    expect(html).not.toContain("Open the screen");
  });

  it("falls back to your computer when device_name is null", () => {
    const html = rail(device(null));
    expect(html).toContain("Browser on your computer");
    expect(html).not.toContain("Open the screen");
  });

  it("a base device Machine shows no browser at all", () => {
    expect(rail({ ...device("MacBook Pro"), profile: "base" })).not.toContain("Browser on");
  });
});

describe("the rail's earlier plans", () => {
  const steps = [{ id: "s1", title: "Reply", state: "done" as const, attempt: 1 }];
  const html = (earlierPlans: { id: string; label: string; steps: typeof steps }[]) =>
    renderToStaticMarkup(
      <TaskRail
        steps={steps}
        earlierPlans={earlierPlans}
        artifacts={[]}
        onOpenScreen={() => {}}
        onOpenArtifact={() => {}}
      />,
    );

  it("a thread with no superseded plan shows no Earlier plans row", () => {
    expect(html([])).not.toContain("Earlier plans");
  });

  it("a superseded revision shows the row with its count", () => {
    const out = html([{ id: "t-rev0", label: "Superseded revision 1", steps }]);
    expect(out).toContain("Earlier plans");
  });
});
