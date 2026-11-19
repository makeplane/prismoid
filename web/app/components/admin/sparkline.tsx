/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One quiet line over the spend window - inline SVG off the pure series helpers, no chart lib.
// A single series, so there is one honest ceiling and no stacked/unstacked question to get
// wrong (see lib/spend-series for the two the grouped chart has to choose between).

import { Money } from "~/components/kit";
import { dailyTotals, linePoints, type SpendPoint } from "~/lib/spend-series";

const W = 240;
const H = 38;

export function Sparkline({ points }: { points: SpendPoint[] }) {
  const totals = dailyTotals(points);
  if (totals.length === 0) {
    return <p className="text-muted-foreground text-caption-md-regular">No spend in the window.</p>;
  }
  const values = totals.map((t) => t.value);
  const max = Math.max(...values);
  const first = totals[0].day;
  const last = totals[totals.length - 1].day;

  return (
    <div className="space-y-1">
      <svg
        viewBox={`0 0 ${W} ${H + 2}`}
        preserveAspectRatio="none"
        className="text-primary h-10 w-full"
        role="img"
        aria-label={`Daily spend, ${first} to ${last}`}
      >
        <polyline
          points={linePoints(values, max, W, H)}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <p className="text-muted-foreground flex justify-between text-caption-md-regular tabular-nums">
        <span>{first}</span>
        <span>
          peak <Money usd={max} muted />
          /day
        </span>
        <span>{last}</span>
      </p>
    </div>
  );
}
