/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A finished task opens before its stream has replayed a single frame. That stretch reads as
// loading; "No transcript for this task." is only for a task whose replay really held nothing.
import { cleanup, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "~/lib/api/types.gen";
import { initialView, type TaskView } from "~/lib/task-view";
import { Transcript } from "./transcript";

afterEach(cleanup);

const detail = {
  id: "t1",
  status: "delivered",
  can_edit: true,
  cost: { total_usd: "0" },
  plan: { steps: [] },
  turn: 1,
  goal: "hi",
} as unknown as components["schemas"]["TaskDetailOut"];

function renderSettled(view: TaskView) {
  render(
    <MemoryRouter>
      <Transcript
        markdownActions={{}}
        scrollApi={createRef()}
        ancestors={[]}
        ancestorsError={false}
        feed={{
          view,
          artifacts: [],
          onOpenArtifact: vi.fn(),
          shared: false,
          questionOpen: false,
          onDeclineProposal: vi.fn(),
        }}
        tail={{
          view,
          live: false,
          gated: false,
          held: false,
          liveNote: null,
          pause: null,
          answerText: "",
          retrying: false,
        }}
      />
    </MemoryRouter>,
  );
}

describe("a settled task with an empty feed", () => {
  it("shows a loading state until the first frame is replayed", () => {
    renderSettled(initialView(detail));
    expect(screen.getByText("Loading the transcript…")).toBeDefined();
    expect(screen.queryByText("No transcript for this task.")).toBeNull();
  });

  it("says there is no transcript only once frames were replayed and none drew", () => {
    renderSettled({ ...initialView(detail), appliedSeq: 4 });
    expect(screen.getByText("No transcript for this task.")).toBeDefined();
    expect(screen.queryByText("Loading the transcript…")).toBeNull();
  });
});
