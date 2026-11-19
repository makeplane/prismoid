/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { BadgeCheck, ShieldAlert, ShieldQuestion } from "lucide-react";
import type { Verdict } from "~/lib/api/events";
import { Pill } from "./primitives";
import type { Tone } from "./tone";

// Verdicts are first-class UI - the product's whole claim is *verified* work. `unverified` renders as
// honest amber (warning), never hidden or dressed up as a pass.
// Labels wear title case ("Verified", not "verified") - a verdict is a stamp, and the
// lowercase word next to capitalized meta chips read as an afterthought.
const MAP: Record<Verdict, { label: string; tone: Tone; Icon: typeof BadgeCheck }> = {
  pass: { label: "Verified", tone: "success", Icon: BadgeCheck },
  // "Failed check", never a bare "Failed": the thing was made and delivered; its check failed.
  fail: { label: "Failed check", tone: "danger", Icon: ShieldAlert },
  unverified: { label: "Unverified", tone: "warning", Icon: ShieldQuestion },
};

export function VerdictBadge({
  verdict,
  className,
  showIcon = true,
}: {
  verdict: Verdict | string | null | undefined;
  className?: string;
  showIcon?: boolean;
}) {
  if (!verdict) {
    return (
      <Pill tone="muted" className={className}>
        Pending
      </Pill>
    );
  }
  const { label, tone, Icon } = MAP[verdict as Verdict] ?? MAP.unverified;
  return (
    <Pill tone={tone} icon={showIcon ? Icon : undefined} className={className}>
      {label}
    </Pill>
  );
}
