/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { lazy, Suspense, useEffect, useState } from "react";
import { LoadingState, QuietDisclosure, ToggleChip } from "~/components/kit";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@makeplane/propel/components/table";
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { withPageCsp } from "./page-csp";

// Lazy exactly as kit/markdown.tsx:16 does for the same block: shiki rides only its own
// chunk, and the Suspense fallback <pre> is what static renders (tests) pin.
const LazyAnswerCodeBlock = lazy(() => import("~/components/kit/answer-code-block"));

type PreviewOut = components["schemas"]["PreviewOut"];

/**
 * The binary-artifact preview: render sidecars extracted INSIDE
 * the frame - a workbook as its computed grid, a Word file as its blocks, a deck as its slides,
 * a code archive as its files, a page as the page itself. One component, one fetch, a switch on
 * `preview.kind`; absence or failure degrades silently - the DownloadCard below is always the
 * floor. Replaces the sheet-only `sheet-preview.tsx` (same mechanism, every kind).
 */
export default function PreviewView({ artifactId }: { artifactId: string }) {
  const [state, setState] = useState<{ preview?: PreviewOut; loading: boolean }>({
    loading: true,
  });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true });
    unwrap(
      api.GET("/v1/artifacts/{artifact_id}/preview", {
        params: { path: { artifact_id: artifactId } },
      }),
    )
      .then((preview) => !cancelled && setState({ preview, loading: false }))
      .catch(() => !cancelled && setState({ loading: false }));
    return () => {
      cancelled = true;
    };
  }, [artifactId]);

  if (state.loading) return <LoadingState label="Loading preview…" />;
  if (!state.preview) return null;
  return <PreviewBody preview={state.preview} />;
}

/** The presentational half, exported for render pins - no fetch, no effects. */
export function PreviewBody({ preview }: { preview: PreviewOut }) {
  switch (preview.kind) {
    case "sheet":
      return preview.sheets.length ? <SheetCase preview={preview} /> : null;
    case "document":
      return preview.blocks.length ? <DocumentCase preview={preview} /> : null;
    case "deck":
      return preview.slides.length ? <DeckCase preview={preview} /> : null;
    case "code":
      return preview.files.length ? <CodeCase preview={preview} /> : null;
    case "page":
      return preview.html ? <PageCase html={preview.html} /> : null;
    default:
      return null; // a future kind this build doesn't know - the DownloadCard floor holds
  }
}

// ── sheet - the M4 table, verbatim ────────────────────────────────────────────────────────────

function SheetCase({ preview }: { preview: PreviewOut }) {
  const [sheet, setSheet] = useState(0);
  const current = preview.sheets[Math.min(sheet, preview.sheets.length - 1)];
  const [header, ...body] = current.rows;

  return (
    <div className="space-y-3">
      {preview.sheets.length > 1 && (
        <div className="flex flex-wrap items-center gap-2">
          {preview.sheets.map((s, i) => (
            <ToggleChip key={s.name} active={i === sheet} onClick={() => setSheet(i)}>
              {s.name}
            </ToggleChip>
          ))}
        </div>
      )}
      {/* isolate: Propel's sticky z-20 header cells must not paint over the shell's sticky
          z-10 topbar as the page scrolls */}
      <div className="isolate">
        <Table variant="table">
          {header && (
            <TableHeader>
              <TableRow>
                {header.map((cell, i) => (
                  <TableHead key={i} label={cell == null ? "" : String(cell)} />
                ))}
              </TableRow>
            </TableHeader>
          )}
          <TableBody>
            {body.map((row, r) => (
              <TableRow key={r}>
                {row.map((cell, c) => (
                  <TableCell key={c}>
                    {typeof cell === "number" ? (
                      <span className="tabular-nums">{cell.toLocaleString("en-US")}</span>
                    ) : (
                      (cell ?? "")
                    )}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <p className="text-muted-foreground text-caption-md-regular">
        Sheet {current.name} · {current.dims}
        {current.truncated ? " · head shown - download for the whole workbook" : ""} · values
        computed in the sandbox
      </p>
    </div>
  );
}

// ── document - extracted blocks, reading-column quiet ─────────────────────────────────────────

const BLOCK_CLASS: Record<string, string> = {
  h1: "text-h4-regular font-semibold mt-4 first:mt-0",
  h2: "text-h5-regular font-semibold mt-3 first:mt-0",
  h3: "text-body-md-regular font-semibold mt-2 first:mt-0",
  p: "text-body-sm-regular leading-6",
  li: "text-body-sm-regular leading-6 pl-5 relative before:absolute before:left-1 before:content-['–']",
};

function DocumentCase({ preview }: { preview: PreviewOut }) {
  return (
    <div className="space-y-2">
      {preview.blocks.map((block, i) =>
        block.table ? (
          <div key={i} className="isolate">
            <Table variant="table">
              <TableBody>
                {block.table.map((row, r) => (
                  <TableRow key={r}>
                    {row.map((cell, c) => (
                      <TableCell key={c}>
                        {r === 0 ? <span className="font-medium">{cell}</span> : cell}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p key={i} className={BLOCK_CLASS[block.style ?? "p"] ?? BLOCK_CLASS.p}>
            {block.text}
          </p>
        ),
      )}
      <p className="text-muted-foreground text-caption-md-regular">
        Extracted in the sandbox
        {preview.truncated ? " · beginning shown - download for the whole document" : ""}
      </p>
    </div>
  );
}

// ── deck - slides as quiet cards ──────────────────────────────────────────────────────────────

function DeckCase({ preview }: { preview: PreviewOut }) {
  return (
    <div className="space-y-3">
      {preview.slides.map((slide, i) => (
        <div key={i} className="ring-foreground/10 bg-card rounded-lg p-4 ring-1">
          <p className="text-muted-foreground mb-1 text-caption-md-regular tabular-nums">
            Slide {i + 1}
          </p>
          {slide.title && <p className="text-body-sm-regular font-semibold">{slide.title}</p>}
          {slide.lines.map((line, l) => (
            <p key={l} className="text-muted-foreground mt-1 text-body-sm-regular leading-6">
              {line}
            </p>
          ))}
        </div>
      ))}
      <p className="text-muted-foreground text-caption-md-regular">
        Slide text extracted in the sandbox{preview.truncated ? " · first slides shown" : ""} -
        download for the designed deck
      </p>
    </div>
  );
}

// ── code - the archive's files, shown as code ─────────────────────────────────────────────────

function CodeCase({ preview }: { preview: PreviewOut }) {
  return (
    <div className="space-y-2">
      {preview.files.map((file) => (
        <QuietDisclosure key={file.path} label={file.path} defaultOpen={preview.files.length === 1}>
          {file.text != null ? (
            // The answer path's code card: highlighting + the copy button, with
            // the file extension as the grammar hint (unknown extensions read as text).
            <Suspense
              fallback={
                <pre className="overflow-x-auto font-mono text-caption-md-regular leading-5">
                  {file.text}
                </pre>
              }
            >
              <LazyAnswerCodeBlock
                code={file.text}
                language={file.path.split(".").pop()?.toLowerCase() ?? ""}
              />
            </Suspense>
          ) : (
            <p className="text-muted-foreground text-caption-md-regular">
              Binary or large file - in the download.
            </p>
          )}
        </QuietDisclosure>
      ))}
      <p className="text-muted-foreground text-caption-md-regular">
        {preview.files.length} file(s){preview.truncated ? " · first files shown" : ""} · the
        archive carries its own tests
      </p>
    </div>
  );
}

// ── page - the page itself, in a fully sandboxed iframe ───────────────────────────────────────

/**
 * The safety story, in one place:
 *  - `sandbox="allow-scripts"` and NOTHING else - no `allow-same-origin`, ever. The page runs
 *    in a unique opaque origin: it cannot read our cookies or storage, cannot reach the app's
 *    DOM, cannot navigate the top window, cannot open popups or submit forms.
 *  - A CSP <meta> injected at the DOCUMENT front (`withPageCsp`, in ./page-csp - shared with
 *    rendition-view's stricter sheet cage) - ahead of anything the parser could fetch, even
 *    content planted before a literal <head> tag: no network of any kind (`default-src 'none'`),
 *    inline style/script only, data:-URI images. The page skill builds self-contained pages;
 *    this enforces the same law on any bytes that reach the reader by other roads.
 *  - `referrerPolicy="no-referrer"` - the page learns nothing about where it is shown.
 */
function PageCase({ html }: { html: string }) {
  return (
    <div className="space-y-2">
      <div
        className={
          /* token-ok: a page preview renders on its own white canvas in both themes */ "ring-foreground/10 overflow-hidden rounded-lg bg-white ring-1"
        }
      >
        <iframe
          title="Page preview (sandboxed)"
          srcDoc={withPageCsp(html)}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          loading="lazy"
          className="h-[75dvh] max-h-[56rem] w-full"
        />
      </div>
      <QuietDisclosure label="View source">
        <pre className="bg-muted/50 ring-foreground/10 overflow-x-auto rounded-lg p-3 font-mono text-caption-md-regular leading-5 ring-1">
          {html}
        </pre>
      </QuietDisclosure>
      <p className="text-muted-foreground text-caption-md-regular">
        Running sandboxed - no network, no cookies, its own origin · download for the file
      </p>
    </div>
  );
}
