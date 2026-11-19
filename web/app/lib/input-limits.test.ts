/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The attachment limits, pinned to the API contract they mirror, and the words around them.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  MAX_INPUTS,
  MAX_INPUT_BYTES,
  admitFiles,
  inputsProblem,
  overLimitMessage,
} from "./input-limits";

type Schema = {
  properties: Record<string, { maxItems?: number; maxLength?: number }>;
};
const spec = JSON.parse(
  readFileSync(new URL("../../../docs/api/openapi.json", import.meta.url), "utf8"),
) as { components: { schemas: Record<string, Schema> } };
const schema = (name: string) => spec.components.schemas[name];

describe("the limits mirror the API contract", () => {
  it("a task and a follow-up take MAX_INPUTS attachments", () => {
    expect(schema("TaskCreateIn").properties.inputs.maxItems).toBe(MAX_INPUTS);
    expect(schema("FollowUpIn").properties.inputs.maxItems).toBe(MAX_INPUTS);
  });

  it("one attachment is MAX_INPUT_BYTES, as the contract's base64 ceiling says", () => {
    // The server's ceiling: the base64 of the largest file, plus room for line-wrapping
    // (serializers/tasks.py MAX_INPUT_BASE64_CHARS).
    const ceiling = 4 * Math.ceil(MAX_INPUT_BYTES / 3) + Math.floor(MAX_INPUT_BYTES / 32);
    expect(schema("TaskInputIn").properties.content.maxLength).toBe(ceiling);
  });
});

describe("admitFiles", () => {
  it("fits what has room and names the rest, in the order chosen", () => {
    const files = Array.from({ length: 12 }, (_, i) => `f${i}.txt`);
    const { fit, over } = admitFiles(0, files);
    expect(fit).toHaveLength(MAX_INPUTS);
    expect(over).toEqual(["f10.txt", "f11.txt"]);
    expect(admitFiles(MAX_INPUTS, ["a"])).toEqual({ fit: [], over: ["a"] });
    expect(admitFiles(3, ["a", "b"])).toEqual({ fit: ["a", "b"], over: [] });
  });
});

describe("the messages", () => {
  it("names the files left off, a few then a count", () => {
    expect(overLimitMessage(["a.pdf"])).toBe(
      "A message takes up to 10 files. Not attached: a.pdf.",
    );
    expect(overLimitMessage(["a", "b", "c", "d", "e"])).toBe(
      "A message takes up to 10 files. Not attached: a, b, c and 2 more.",
    );
  });

  it("reads the server's 422 on attachments as a sentence about the files", () => {
    expect(
      inputsProblem({ inputs: "List should have at most 10 items after validation, not 12" }, []),
    ).toBe("A message takes up to 10 files. Remove some and send again.");
    expect(
      inputsProblem({ "inputs.1.content": "String should have at most 7154348 characters" }, [
        "a.txt",
        "big.zip",
      ]),
    ).toBe("big.zip is over the 5 MB limit for one file.");
    expect(inputsProblem({ "inputs.0.filename": "Value error, not a name" }, ["x/y"])).toBe(
      "x/y: Value error, not a name",
    );
    expect(inputsProblem({ goal: "too long" }, [])).toBeUndefined();
    expect(inputsProblem(undefined, [])).toBeUndefined();
  });
});
