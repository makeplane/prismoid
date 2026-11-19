/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Section } from "~/components/kit";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@makeplane/propel/components/table";

const LAYERS: { layer: string; dir: string; holds: string; law: string }[] = [
  {
    layer: "Tokens",
    dir: "packages/blocks + app/app.css",
    holds:
      "Propel's tokens with the monochrome accent (@prismoid/blocks), Prismoid's semantic names pointed at them, the hue ramp, markdown styles (ADR-074)",
    law: "Plane's scale, black accent. Semantic tokens only - raw palette is lint-banned.",
  },
  {
    layer: "Blocks",
    dir: "packages/blocks/src/",
    holds:
      "Prismoid's composites on Propel: sidebar, prompt-input, attachment, spinner, bubble, marker, message-scroller, code-block, card, toast, cn (ADR-074)",
    law: "Propel parts and Propel tokens only; Propel components get no className. One subpath per composite, no barrel.",
  },
  {
    layer: "Propel",
    dir: "@makeplane/propel",
    holds:
      "Plane's primitives - buttons, fields, menus, dialogs, tables, tabs, toasts' substrate - imported directly (ADR-074)",
    law: "No className or style; content is a prop; every variant axis is passed. Layout goes on a wrapper.",
  },
  {
    layer: "Kit",
    dir: "app/components/kit/",
    holds: "The house vocabulary - 27 exports through one barrel (index.ts)",
    law: "The ONLY place domain visuals are defined. Typed twice → it becomes a kit primitive.",
  },
  {
    layer: "Layout",
    dir: "app/components/layout/",
    holds: "Shell chrome - sidebar, breadcrumbs, settings, auth frame, error screen",
    law: "Composes Propel + blocks + kit; adds no new visual vocabulary.",
  },
  {
    layer: "Features",
    dir: "app/components/{artifact,tasks,composer,icon-picker}/",
    holds:
      "Artifact renderers (one per kind), the task browser, the gate card, the composer, the project face picker",
    law: "Compose Propel + blocks + kit. Renderers dispatch on the server's render hint, never a client type list.",
  },
  {
    layer: "Routes",
    dir: "app/routes/ (map: app/routes.ts)",
    holds: "One directory per screen; routes.ts is the entire app in one file",
    law: "Composition only. Route-local pieces stay in their route dir, never imported across routes.",
  },
  {
    layer: "Contract",
    dir: "web/DESIGN.md",
    holds: "The written law this page renders - type roles, rhythm, hairlines, radius, tone",
    law: "Change a rule by changing the doc and the kit in the same commit.",
  },
];

// ── The census: every component, its file, its role ──────────────────────────

export function MapSection() {
  return (
    <section id="map">
      <Section title="Map - how it's organised">
        <p className="text-muted-foreground max-w-2xl text-body-sm-regular">
          Eight homes, one dependency direction: routes compose features, features compose kit +
          blocks + Propel, everything reads the tokens. Lower layers never import upward.
        </p>
        {/* Propel's cells truncate to one line; the prose columns here opt back into wrapping. */}
        <Table variant="table">
          <TableHeader>
            <TableRow>
              <TableHead label="Layer" />
              <TableHead label="Where" />
              <TableHead label="What lives there" />
              <TableHead label="The law" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {LAYERS.map((l) => (
              <TableRow key={l.layer}>
                <TableCell>
                  <span className="font-medium">{l.layer}</span>
                </TableCell>
                <TableCell>
                  <code className="text-muted-foreground font-mono text-caption-md-regular whitespace-normal">
                    {l.dir}
                  </code>
                </TableCell>
                <TableCell>
                  <span className="text-muted-foreground whitespace-normal">{l.holds}</span>
                </TableCell>
                <TableCell>
                  <span className="text-muted-foreground whitespace-normal">{l.law}</span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Section>
    </section>
  );
}
