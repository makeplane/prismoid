/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The bot's face - ProjectIcon's vocabulary applied to bots (ADR-045). One deliberate
// difference, and it is the whole file: a bot with NO stored face still gets its OWN look -
// the default glyph is the bot mark and the default hue derives from the name, so a fresh
// roster is a wall of distinct cards rather than eight grey squares. Deterministic and
// stable (DESIGN's hue law: never hand-picked, always an index), and a stored face wins.

import { nameHue } from "~/components/kit/tone";
import { ProjectIcon } from "./project-icon";

// The name-to-hue hash lives with the ramp now (kit/tone.ts - this
// file and the tree's agent chip had drifted-by-literal twins). Re-exported here because
// the icon-picker barrel and the roster cards read it as this module's vocabulary.
export { nameHue };

export function BotAvatar({
  name,
  icon,
  hue,
  size = "md",
  className,
}: {
  name: string;
  icon: string | null | undefined;
  hue: number | null | undefined;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  return (
    <ProjectIcon
      icon={icon ?? "lucide:bot"}
      hue={hue ?? nameHue(name)}
      size={size}
      className={className}
    />
  );
}
