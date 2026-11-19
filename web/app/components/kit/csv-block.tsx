/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// ```csv → a rendered table preview (ADR-053). First row is the header, the render caps at
// fifty rows with an honest "+N more" line (never silent truncation),
// and the streaming variant holds back the partial tail line so cells never flicker
// half-typed. Copy hands over the raw CSV, verbatim.
import { parseCsv, trimOpenTail } from "./csv";
import { CopyAction } from "./draft-actions";
import { SpecialBlockShell, type SpecialBlockProps } from "./special-blocks";
import { TableIcon } from "lucide-react";

const ROW_CAP = 50;

export function CsvBlock({ raw, streaming }: SpecialBlockProps) {
  const source = streaming ? trimOpenTail(raw) : raw;
  // parseCsv is the ONE cap: asked for ROW_CAP+1 rows, so after the header destructure the
  // body holds at most ROW_CAP - `more` counts the rest.
  const { rows, more } = parseCsv(source, ROW_CAP + 1);
  const [head, ...body] = rows;

  return (
    <SpecialBlockShell
      block="csv"
      icon={TableIcon}
      title="csv"
      meta={
        body.length > 0 && (
          <span className="font-regular">
            · {body.length + more} row{body.length + more === 1 ? "" : "s"}
          </span>
        )
      }
      actions={
        !streaming && (
          <CopyAction onCopy={() => navigator.clipboard.writeText(raw)} label="Copy CSV" />
        )
      }
    >
      <div className="overflow-x-auto px-3 py-2">
        {head ? (
          <table className="my-0!">
            <thead>
              <tr>
                {head.map((cell, i) => (
                  <th key={i}>{cell}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-muted-foreground my-1 text-caption-md-regular">Waiting for rows…</p>
        )}
        {more > 0 && (
          <p className="text-muted-foreground mt-1 mb-0 text-caption-md-regular">
            +{more} more row{more === 1 ? "" : "s"} in the source
          </p>
        )}
      </div>
    </SpecialBlockShell>
  );
}
