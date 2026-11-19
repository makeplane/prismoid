/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef, useState } from "react";
import { Pill, Section, ToggleChip } from "~/components/kit";
import { Avatar } from "@makeplane/propel/components/avatar";
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { Switch } from "@makeplane/propel/components/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@makeplane/propel/components/table";
import { Card, CardContent } from "@prismoid/blocks/card";
import { Specimen } from "./shared";

// Plane's corners: Tailwind's default steps, which Propel keeps.
const STEPS = [
  { cls: "rounded-xs", name: "xs", value: "0.125rem", role: "grips · kbd · micro-chrome" },
  { cls: "rounded-sm", name: "sm", value: "0.25rem", role: "inline code · calendar chips" },
  {
    cls: "rounded-md",
    name: "md",
    value: "0.375rem",
    role: "controls - buttons · inputs · menu items",
  },
  {
    cls: "rounded-lg",
    name: "lg",
    value: "0.5rem",
    role: "surfaces - cards · rows · popovers · columns",
  },
  {
    cls: "rounded-xl",
    name: "xl",
    value: "0.75rem",
    role: "dialogs · command · page-level shells",
  },
  { cls: "rounded-2xl", name: "2xl", value: "1rem", role: "reserved (3xl exists, unused)" },
  {
    cls: "rounded-pill",
    name: "pill",
    value: "= md",
    role: "text chips - Pill · ToggleChip · Badge",
  },
  { cls: "rounded-full", name: "full", value: "∞", role: "true circles & tracks ONLY" },
];

/** A probe square that reports its own live computed corner. */
function StepBox({ cls, name, value }: { cls: string; name: string; value: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [px, setPx] = useState("");
  useEffect(() => {
    if (!ref.current) return;
    const v = getComputedStyle(ref.current).borderTopLeftRadius;
    setPx(name === "full" ? "∞" : `${Math.round(parseFloat(v) * 10) / 10}px`);
  }, [name]);
  return (
    <div className="flex flex-col items-center gap-1.5">
      <div
        ref={ref}
        className={`bg-primary/15 ring-foreground/10 size-14 ring-1 ring-inset ${cls}`}
      />
      <span className="text-caption-md-regular font-medium">{name}</span>
      <span className="text-muted-foreground text-caption-md-regular tabular-nums">
        {value} · {px}
      </span>
    </div>
  );
}

function RadiusLab() {
  return (
    <div className="space-y-5">
      <p className="text-muted-foreground max-w-2xl text-caption-md-regular">
        Plane's corners: Propel keeps Tailwind's default steps, so every corner in the product is
        one of these (ADR-074). Circles stay circles on purpose.
      </p>
      <div className="flex flex-wrap items-start gap-6">
        {STEPS.map((s) => (
          <StepBox key={s.cls} {...s} />
        ))}
      </div>
      <Table variant="table">
        <TableHeader>
          <TableRow>
            <TableHead label="Token" />
            <TableHead label="Value" />
            <TableHead label="Role" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {STEPS.map((s) => (
            <TableRow key={s.cls}>
              <TableCell>
                <code className="bg-muted rounded-sm px-1.5 py-0.5 font-mono text-caption-md-regular">
                  {s.cls}
                </code>
              </TableCell>
              <TableCell>
                <span className="text-muted-foreground tabular-nums">{s.value}</span>
              </TableCell>
              <TableCell>
                <span className="text-muted-foreground">{s.role}</span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Specimen
        label="live specimens"
        file="app/app.css"
        note="The circle law holds: the avatar, dot and switch stay geometric."
      >
        <Button variant="primary" size="sm" stretch="auto" label="Button" />
        <Button variant="secondary" size="sm" stretch="auto" label="Secondary" />
        <div className="w-36">
          <InputField size="md" orientation="vertical" placeholder="Input" aria-label="Input" />
        </div>
        <Pill tone="info">pill</Pill>
        <Badge variant="neutral" size="sm" label="badge" />
        <ToggleChip active>chip</ToggleChip>
        <Card className="w-36" size="sm">
          <CardContent className="text-muted-foreground text-caption-md-regular">
            card surface
          </CardContent>
        </Card>
        <Avatar size="sm" alt="Pat Chen" fallback="PC" />
        <Switch size="md" defaultChecked aria-label="A switch" />
      </Specimen>
    </div>
  );
}

export function RadiusSection() {
  return (
    <section id="radius">
      <Section title="Radius - Plane's scale">
        <RadiusLab />
      </Section>
    </section>
  );
}
