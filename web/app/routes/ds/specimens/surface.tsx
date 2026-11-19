/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Section } from "~/components/kit";

export function SurfaceSection() {
  return (
    <section id="surface">
      <Section title="Surfaces - hairlines, elevation, selection">
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="ring-foreground/10 rounded-lg p-4 ring-1 shadow-raised-100">
            <p className="text-body-sm-regular font-medium">rest</p>
            <p className="text-muted-foreground text-caption-md-regular">
              ring-1 ring-foreground/10 + shadow-raised-100 - an object standing on the page.
            </p>
          </div>
          <div className="ring-foreground/10 rounded-lg p-4 shadow-raised-300 ring-1">
            <p className="text-body-sm-regular font-medium">raised</p>
            <p className="text-muted-foreground text-caption-md-regular">
              shadow-raised-300 - mid-drag cards, the record peek, docked bars.
            </p>
          </div>
          <div className="ring-primary/40 rounded-lg p-4 ring-2">
            <p className="text-body-sm-regular font-medium">selected (floating)</p>
            <p className="text-muted-foreground text-caption-md-regular">
              ring-2 ring-primary/40 replaces the rest ring; shadow never changes.
            </p>
          </div>
        </div>
        <div className="bg-muted flex items-center gap-3 px-4 py-2.5 shadow-[inset_2px_0_0_0_var(--color-primary)]">
          <span className="text-body-sm-regular">selected (flow row)</span>
          <span className="text-muted-foreground text-caption-md-regular">
            bg-muted + an inset leading accent edge - travels with sticky cells, shifts no layout.
          </span>
        </div>
      </Section>
    </section>
  );
}
