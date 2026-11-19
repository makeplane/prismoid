/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArrowLeft } from "lucide-react";
import { Link } from "react-router";
import { PageHeader } from "~/components/kit";
import { ModeToggle } from "~/components/layout/mode-toggle";
import { BlocksSection } from "./specimens/blocks";
import { CensusSection } from "./specimens/census";
import { ColorSection } from "./specimens/color";
import { KitSection } from "./specimens/kit";
import { MapSection } from "./specimens/map";
import { PropelSection } from "./specimens/propel";
import { RadiusSection } from "./specimens/radius";
import { RenderersSection } from "./specimens/renderers";
import { SurfaceSection } from "./specimens/surface";
import { TypeSection } from "./specimens/type";

export function meta() {
  return [{ title: "Design system · Prismoid" }];
}

// ─────────────────────────────────────────────────────────────────────────────
// /ds - the design system, whole. An easter egg by design: registered in the app
// map but linked from nowhere. Everything the product is made of renders here -
// with its home in the codebase - on Plane's scale with the black accent.
// ─────────────────────────────────────────────────────────────────────────────

const TOC = [
  ["map", "Map"],
  ["radius", "Radius"],
  ["color", "Color"],
  ["type", "Type"],
  ["surface", "Surfaces"],
  ["kit", "Kit"],
  ["propel", "Propel"],
  ["blocks", "Blocks"],
  ["renderers", "Renderers"],
  ["census", "Census"],
] as const;

export default function DesignSystem() {
  return (
    <div id="top" className="mx-auto max-w-6xl space-y-10 px-8 py-8">
      <div className="flex items-start justify-between">
        <PageHeader
          title="Design system"
          description="You found it. The whole product surface - tokens, Propel, blocks, kit, renderers - with its home in the codebase, on Plane's scale. Linked from nowhere, on purpose."
        />
        <div className="flex items-center gap-2">
          <Link
            to="/"
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 text-body-sm-regular"
          >
            <ArrowLeft className="size-4" /> App
          </Link>
          <ModeToggle />
        </div>
      </div>

      <nav aria-label="Sections" className="flex flex-wrap gap-2">
        {TOC.map(([id, label]) => (
          <a
            key={id}
            href={`#${id}`}
            className="ring-foreground/10 text-muted-foreground hover:text-foreground rounded-pill px-2.5 py-0.5 text-caption-md-regular ring-1 ring-inset transition-colors"
          >
            {label}
          </a>
        ))}
      </nav>

      <MapSection />

      <RadiusSection />

      <ColorSection />

      <TypeSection />

      <SurfaceSection />

      <KitSection />

      <PropelSection />

      <BlocksSection />

      <RenderersSection />

      <CensusSection />
    </div>
  );
}
