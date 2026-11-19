/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { HUE_TINT, Pill, Section } from "~/components/kit";
import { Specimen, TONES } from "./shared";

const SURFACES = [
  { name: "background", cls: "bg-background" },
  { name: "card", cls: "bg-card" },
  { name: "popover", cls: "bg-popover" },
  { name: "muted", cls: "bg-muted" },
  { name: "accent", cls: "bg-accent" },
  { name: "secondary", cls: "bg-secondary" },
  { name: "primary", cls: "bg-primary" },
];

export function ColorSection() {
  return (
    <section id="color">
      <Section title="Color - semantic tokens only">
        <div className="space-y-4">
          <Specimen label="surfaces" file="app/app.css">
            {SURFACES.map((s) => (
              <span
                key={s.name}
                className="inline-flex items-center gap-1.5 text-caption-md-regular"
              >
                <span
                  className={`ring-foreground/20 size-6 rounded-md ring-1 ring-inset ${s.cls}`}
                />
                {s.name}
              </span>
            ))}
          </Specimen>
          <Specimen
            label="status tones"
            file="kit/tone.ts"
            note="tone.ts is the only tint recipe: border /25 · wash /10 · text full."
          >
            {TONES.map((t) => (
              <Pill key={t} tone={t}>
                {t}
              </Pill>
            ))}
          </Specimen>
          <Specimen
            label="categorical hues"
            file="kit/tone.ts"
            note="User-data kind colors (select options, chart series) - hue = option index % 8. Never status."
          >
            {HUE_TINT.map((cls, i) => (
              <span
                key={cls}
                className={`rounded-pill border px-2 py-0.5 text-caption-md-regular font-medium ${cls}`}
              >
                hue-{i + 1}
              </span>
            ))}
          </Specimen>
          <Specimen label="hairline + focus" file="app/app.css">
            <span className="border-border rounded-md border px-3 py-1.5 text-caption-md-regular">
              border
            </span>
            <span className="ring-foreground/10 rounded-md px-3 py-1.5 text-caption-md-regular ring-1">
              ring-foreground/10
            </span>
            <span className="ring-ring rounded-md px-3 py-1.5 text-caption-md-regular ring-2">
              focus ring
            </span>
          </Specimen>
        </div>
      </Section>
    </section>
  );
}
