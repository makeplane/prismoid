/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A held fork (ADR-018: born held, parked until its first direction): the transcript names the
// state and links the parent it forked from, and the box offers no Stop because nothing runs.
import { cleanup, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { initialView } from "~/lib/task-view";
import type { components } from "~/lib/api/types.gen";
import { ComposerBar } from "./composer-bar";
import { Transcript, type TailProps } from "./transcript";
import type { Ancestor } from "./thread";

afterEach(cleanup);

type TaskDetailOut = components["schemas"]["TaskDetailOut"];

const detail = {
  id: "fork-1",
  status: "created",
  can_edit: true,
  cost: { total_usd: "0" },
  plan: { steps: [] },
  turn: 1,
  goal: "hi",
} as unknown as TaskDetailOut;
const view = initialView(detail);
const parent: Ancestor = {
  taskId: "parent-1",
  goal: "Plan the offsite",
  steps: [],
  feed: [],
  artifacts: [],
};

function renderTranscript(ancestors: Ancestor[], held: boolean) {
  const tail: TailProps = {
    view,
    live: true,
    gated: false,
    held,
    liveNote: null,
    pause: null,
    answerText: "",
    retrying: false,
  };
  render(
    <MemoryRouter>
      <Transcript
        markdownActions={{}}
        scrollApi={createRef()}
        ancestors={ancestors}
        ancestorsError={false}
        feed={{
          view,
          artifacts: [],
          onOpenArtifact: vi.fn(),
          shared: false,
          questionOpen: false,
          onDeclineProposal: vi.fn(),
        }}
        tail={tail}
      />
    </MemoryRouter>,
  );
}

describe("a held fork's transcript", () => {
  it("says it is held, that nothing started, and what starts it", () => {
    renderTranscript([parent], true);
    const note = screen.getByRole("status");
    expect(note.textContent).toContain("Held.");
    expect(note.textContent).toContain("has not started");
    expect(note.textContent).toContain("Send a direction");
  });

  it("names the parent in the 'forked from' divider and links to it", () => {
    renderTranscript([parent], true);
    const link = screen.getByRole("link", { name: "Plan the offsite" });
    expect(link.getAttribute("href")).toBe("/tasks/parent-1");
  });

  it("a task with no parent draws no divider", () => {
    renderTranscript([], false);
    expect(screen.queryByText(/forked from/)).toBeNull();
  });
});

function renderBar(held: boolean) {
  render(
    <MemoryRouter>
      <ComposerBar
        barRef={createRef()}
        detail={detail}
        view={view}
        gate={null}
        live
        held={held}
        steerNote={null}
        spendUsd="0"
        typers={[]}
        stale={null}
        sending={false}
        dials={{} as never}
        dockRef={createRef()}
        composerApi={{ current: null }}
        placeholder="Where should this branch go?"
        onResolved={vi.fn()}
        onOpenScreen={vi.fn()}
        onDraft={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
        onSendAnyway={vi.fn()}
        onDiscardStale={vi.fn()}
      />
    </MemoryRouter>,
  );
}

describe("the held fork's composer", () => {
  it("offers no Stop: nothing runs", () => {
    renderBar(true);
    expect(screen.queryByRole("button", { name: "Stop the task" })).toBeNull();
  });

  it("a running task still offers Stop", () => {
    renderBar(false);
    expect(screen.getByRole("button", { name: "Stop the task" })).toBeDefined();
  });
});
