/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The façade drift test. `events.ts` is a hand-kept typed façade over
 *  docs/api/events.schema.json - deliberate (generated discriminators don't narrow), but it
 *  already drifted once (ArtifactSnapshot was missing path/offset/content). This pins the
 *  variant SET both ways: a wire event the façade doesn't know, or a façade event the wire
 *  no longer speaks, fails here instead of at a customer. */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { ALL_EVENT_TYPES } from "./events";

type SchemaDef = {
  properties?: { type?: { const?: string; enum?: string[] } };
};
type EventsSchema = {
  $defs: Record<string, SchemaDef>;
  oneOf: { $ref: string }[];
};

const schema = JSON.parse(
  readFileSync(new URL("../../../../docs/api/events.schema.json", import.meta.url), "utf8"),
) as EventsSchema;

function literalOf(def: SchemaDef): string | null {
  const t = def.properties?.type;
  if (!t) return null;
  if (typeof t.const === "string") return t.const;
  if (Array.isArray(t.enum) && t.enum.length === 1) return t.enum[0];
  return null;
}

describe("events façade ⇄ frozen wire schema", () => {
  const wire = new Set(
    schema.oneOf
      .map((o) => literalOf(schema.$defs[o.$ref.replace("#/$defs/", "")]))
      .filter((x): x is string => x !== null),
  );
  const facade = new Set<string>(ALL_EVENT_TYPES);

  it("the schema's variants resolved to type literals", () => {
    expect(wire.size).toBeGreaterThan(10); // sanity: the resolution walked real defs
  });

  it("every wire variant exists in the façade", () => {
    expect([...wire].filter((t) => !facade.has(t))).toEqual([]);
  });

  it("every façade variant exists on the wire", () => {
    expect([...facade].filter((t) => !wire.has(t))).toEqual([]);
  });
});
