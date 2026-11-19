/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The collection façade drift test - same arrangement as `events.drift.test.ts`: the zod
 * façade in `collection.ts` is hand-kept over `docs/api/collection.schema.json` (exported from
 * the one pydantic definition by `just export-contract`). This pins the FIELD TYPES, LAYOUTS,
 * and envelope keys both ways, so a kind of cell the wire can carry but the viewer cannot
 * render - or vice versa - fails here instead of at a customer. */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { FIELD_TYPES, FILTER_OPS, LAYOUTS, collectionSchema } from "./collection";

type Schema = {
  $defs: Record<string, { enum?: string[]; properties?: Record<string, { enum?: string[] }> }>;
  properties: Record<string, unknown>;
  required?: string[];
};

const schema = JSON.parse(
  readFileSync(new URL("../../../docs/api/collection.schema.json", import.meta.url), "utf8"),
) as Schema;

describe("collection façade ⇄ exported schema", () => {
  it("speaks the same field types", () => {
    expect(new Set(schema.$defs.FieldType?.enum)).toEqual(new Set(FIELD_TYPES));
  });

  it("speaks the same layouts", () => {
    expect(new Set(schema.$defs.ViewLayout?.enum)).toEqual(new Set(LAYOUTS));
  });

  it("carries the same envelope keys", () => {
    const wire = new Set(Object.keys(schema.properties));
    const facade = new Set(Object.keys(collectionSchema.shape));
    expect(facade).toEqual(wire);
  });

  it("carries the same view keys - widths/column_width class of drift fails here", () => {
    const wire = new Set(Object.keys(schema.$defs.CollectionView?.properties ?? {}));
    const facade = new Set(Object.keys(collectionSchema.shape.views.element.shape));
    expect(facade).toEqual(wire);
  });

  it("speaks the same filter ops - the façade is strict, so drift would reject real artifacts", () => {
    expect(new Set(schema.$defs.FilterSpec?.properties?.op?.enum)).toEqual(new Set(FILTER_OPS));
  });

  it("agrees the version is 1", () => {
    const version = schema.properties.collection_version as { const?: number } | undefined;
    expect(version?.const).toBe(1);
  });
});
