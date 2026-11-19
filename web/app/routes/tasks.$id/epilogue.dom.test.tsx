/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The end-of-turn block: a failed receipt keeps "Delivered" (delivery happened) and says so
// in a plain caveat that links to the receipt.
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FeedItem } from "~/lib/task-view";
import { EpilogueBlock } from "./epilogue";

type Item = Extract<FeedItem, { kind: "epilogue" }>;
const item = (over: Partial<Item> = {}): Item => ({
  kind: "epilogue",
  id: "ep1",
  delivered: true,
  remembered: [],
  ...over,
});

afterEach(cleanup);

describe("EpilogueBlock - verification honesty", () => {
  it("a failed receipt still says Delivered, adds a caveat, and opens the receipt", async () => {
    const onOpen = vi.fn();
    render(
      <EpilogueBlock
        item={item({ verdict: "fail", suite: "document-suite", artifactId: "a1" })}
        onOpenArtifact={onOpen}
      />,
    );
    expect(screen.getByText("Delivered")).toBeDefined();
    expect(screen.getByRole("note").textContent).toContain("Checks found problems");
    await userEvent.click(screen.getByRole("button", { name: "See the receipt" }));
    expect(onOpen).toHaveBeenCalledWith("a1");
  });

  it("a passed receipt carries no caveat: it reads Verified, with its suite", () => {
    render(<EpilogueBlock item={item({ verdict: "pass", suite: "document-suite" })} />);
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByText("Verified · document-suite")).toBeDefined();
  });

  it("an answer with nothing to check is the delivery line alone", () => {
    render(<EpilogueBlock item={item()} />);
    expect(screen.queryByText(/Verified|verified|Checks/)).toBeNull();
  });

  it("the time joins Delivered, and tokens are one total", () => {
    render(
      <EpilogueBlock
        item={item({
          receipt: { steps: 2, agents: 2, durationMs: 33_000, tokens: 2500, cachedTokens: 49_200 },
        })}
      />,
    );
    expect(screen.getByText(/in 33s/)).toBeDefined();
    expect(screen.getByText("51.7k tokens")).toBeDefined();
  });

  it("names what checking the answer cost", () => {
    render(<EpilogueBlock item={item({ verdict: "pass", checkUsd: 0.37 })} />);
    expect(screen.getByText(/\$0\.37 to check/)).toBeDefined();
  });
});
