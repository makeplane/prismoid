/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The question dock's contract: numbered rows a direct reply can pick by
// number, drafts that only submit from the LAST question, and required questions that
// refuse silence. The pure core is tested as functions; the card's face as static markup
// (the house idiom - no DOM driver).

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Question } from "~/lib/api/events";
import {
  QuestionDock,
  buildContent,
  matchDirectReply,
  optionRows,
  parseBatch,
  typedDraft,
} from "./question-dock";

const select: Question = {
  id: "runtime",
  prompt: "Where will this run?",
  type: "single_select",
  required: true,
  allow_other: true,
  options: [
    { value: "fc", label: "Existing Firecracker infra", description: null },
    { value: "kvm", label: "Bare-metal Linux w/ KVM", description: null },
    { value: "cloud", label: "Cloud VM (nested virt)", description: null },
  ],
};
const text: Question = {
  id: "notes",
  prompt: "Anything else?",
  type: "text",
  required: false,
  allow_other: false,
  options: [],
};

describe("the pure core", () => {
  it("\u2039 \u203a restore a typed draft, but never echo a clicked option value", () => {
    // typed free text comes back for revision; a clicked option's value stays in the rows
    expect(typedDraft(text, { notes: "ship it Friday" })).toBe("ship it Friday");
    expect(typedDraft(select, { runtime: "kvm" })).toBe("");
    expect(typedDraft(select, { runtime: "bare metal, no KVM" })).toBe("bare metal, no KVM");
    expect(typedDraft(text, {})).toBe("");
  });

  it("booleans imply Yes/No rows; selects render their options", () => {
    expect(optionRows({ ...text, type: "boolean" }).map((r) => r.label)).toEqual(["Yes", "No"]);
    expect(optionRows(select)).toHaveLength(3);
  });

  it("a bare number picks that row; words stay words; out-of-range is text", () => {
    expect(matchDirectReply(select, "2")).toEqual({
      kind: "option",
      value: "kvm",
    });
    expect(matchDirectReply(select, "9")).toEqual({ kind: "text", value: "9" });
    expect(matchDirectReply(select, "I download fire")).toEqual({
      kind: "text",
      value: "I download fire",
    });
  });

  it("buildContent coerces types, omits empty optionals, names missing requireds", () => {
    expect(buildContent([select, text], { runtime: "kvm" })).toEqual({
      ok: true,
      content: { runtime: "kvm" },
    });
    expect(buildContent([select, text], {})).toEqual({
      ok: false,
      missing: ["runtime"],
    });
    expect(buildContent([{ ...text, type: "number", id: "n" }], { n: "42" })).toEqual({
      ok: true,
      content: { n: 42 },
    });
    expect(buildContent([{ ...text, type: "boolean", id: "b" }], { b: "true" })).toEqual({
      ok: true,
      content: { b: true },
    });
  });
});

describe("the card's face", () => {
  const gate = {
    gate_id: "g1",
    title: "A few questions",
    questions: [select, text],
  };

  it("opens on question 1: prompt as title, numbered rows, stepper, own-answer row", () => {
    const html = renderToStaticMarkup(<QuestionDock gate={gate} />);
    expect(html).toContain("Where will this run?");
    expect(html).toContain("1 of 2");
    expect(html).toContain("Bare-metal Linux w/ KVM");
    expect(html).toContain("Or type your own…"); // allow_other's free row
    expect(html).toContain("Skip"); // decline stays reachable
  });

  it("a text question is just the free row - no option rows", () => {
    const html = renderToStaticMarkup(<QuestionDock gate={{ ...gate, questions: [text] }} />);
    expect(html).toContain("Anything else?");
    expect(html).toContain("Type your answer…");
    expect(html).not.toContain("1 of 1"); // a single question needs no stepper
  });
});

describe("one message, every answer", () => {
  it("numbered segments answer their questions - option numbers, labels, or free text", () => {
    expect(parseBatch([select, text], "1: 2; 2: prefer Go")).toEqual({
      runtime: "kvm",
      notes: "prefer Go",
    });
    // inline boundaries and label matching, case-insensitive
    expect(parseBatch([select, text], "1. bare-metal 2. nothing else")).toEqual({
      runtime: "kvm",
      notes: "nothing else",
    });
  });

  it("fewer than two numbered segments is NOT a batch - the current question takes it", () => {
    expect(parseBatch([select, text], "2")).toBeNull();
    expect(parseBatch([select, text], "I download fire")).toBeNull();
    expect(parseBatch([select, text], "1: kvm")).toBeNull();
  });

  it("multi-selects take comma-separated picks inside their segment", () => {
    const multi: Question = { ...select, id: "m", type: "multi_select" };
    expect(parseBatch([multi, text], "1: 1, cloud; 2: done")).toEqual({
      m: ["fc", "cloud"],
      notes: "done",
    });
  });
});
