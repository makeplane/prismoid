/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Badge } from "@makeplane/propel/components/badge";
import { Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { EmptyState, LoadingState, MetaChip, PageHeader, Pill, type Tone } from "~/components/kit";
import { Card, CardContent } from "@prismoid/blocks/card";
import { api, unwrap } from "~/lib/api/client";

// ── Skills: the published registry ────────────────────────────────────────────────────────────

const fetchSkills = () => unwrap(api.GET("/v1/skills"));

const EVAL_TONE: Record<string, Tone> = {
  green: "success",
  passing: "success",
  red: "danger",
  failing: "danger",
};

export function SkillsPanel() {
  const [skills, setSkills] = useState<Awaited<ReturnType<typeof fetchSkills>> | null>(null);
  useEffect(() => {
    fetchSkills()
      .then((all) => {
        // One card per skill name - the highest version.
        const latest = new Map<string, (typeof all)[number]>();
        for (const s of all) {
          const prev = latest.get(s.name);
          if (!prev || s.version > prev.version) latest.set(s.name, s);
        }
        setSkills([...latest.values()].sort((a, b) => a.name.localeCompare(b.name)));
      })
      .catch(() => toast.error("Couldn't load skills."));
  }, []);

  return (
    <div className="space-y-6">
      <PageHeader
        as="h2"
        title="Skills"
        description="The published skill registry, pinned into plans by version."
      />
      {skills === null ? (
        <LoadingState />
      ) : skills.length === 0 ? (
        <EmptyState icon={Sparkles} title="No skills published yet" />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {skills.map((s) => (
            <Card key={s.name} size="sm">
              <CardContent>
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-body-sm-regular font-medium">{s.name}</span>
                    <Badge variant="neutral" size="xs" label={`v${s.version}`} />
                  </div>
                  <Pill tone={EVAL_TONE[s.eval_status] ?? "muted"} className="capitalize">
                    {s.eval_status}
                  </Pill>
                </div>
                <p className="text-muted-foreground mt-2 text-body-sm-regular">{s.description}</p>
                <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1">
                  <MetaChip className="capitalize">{s.capability}</MetaChip>
                  <MetaChip className="capitalize">{s.status}</MetaChip>
                  <MetaChip>{s.origin}</MetaChip>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
