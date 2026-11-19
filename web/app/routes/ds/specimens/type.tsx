/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Section } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { Specimen } from "./shared";

export function TypeSection() {
  return (
    <section id="type">
      <Section title="Type - sizes are roles, not choices">
        <div className="space-y-3">
          <Specimen
            label="hero (landing only)"
            file="routes/home/route.tsx"
            note="The one display size - Inter's opsz axis serves the display cut up here."
          >
            <span className="font-heading text-display font-semibold tracking-tight">
              Good evening.
            </span>
          </Specimen>
          <Specimen
            label="title"
            file="web/DESIGN.md"
            note="One face: InterVariable (opsz build, smoothing + synthesis-none on html). --font-heading stays the parked knob (ADR-017)."
          >
            <span className="font-heading text-h4-regular font-semibold tracking-tight">
              Every screen opens with one of these
            </span>
          </Specimen>
          <Specimen label="section label" file="web/DESIGN.md">
            <span className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
              A quiet uppercase rail
            </span>
          </Specimen>
          <Specimen label="body" file="web/DESIGN.md">
            <span className="text-body-sm-regular">
              Body is text-body-sm-regular: 14px on Plane's 16px root.
            </span>
          </Specimen>
          <Specimen label="meta" file="web/DESIGN.md">
            <span className="text-muted-foreground text-caption-md-regular">
              Meta and labels are text-caption-md-regular. Nothing else exists.
            </span>
          </Specimen>
          <Specimen
            label="micro"
            file="components/badge"
            note="The smallest type belongs to Propel's Badge internals only."
          >
            <Badge variant="neutral" size="xs" label="v2 · latest" />
          </Specimen>
        </div>
      </Section>
    </section>
  );
}
