/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { LoadingState, Markdown, QuietDisclosure } from "~/components/kit";
import { DownloadCard } from "../download-card";
import { MAX_ARCHIVE_BYTES, parseChange, type ChangeArchive } from "./change-archive";
import type { RendererProps } from "./registry";

// Lazy exactly as preview-view's CodeCase does: shiki rides only its own chunk, and the Suspense
// fallback <pre> is what static renders (tests) pin.
const LazyAnswerCodeBlock = lazy(() => import("~/components/kit/answer-code-block"));

// A tokenizer on the main thread is fine for a diff a person reads and a stall for one they
// don't: past this the same text shows in a plain block.
const HIGHLIGHT_CAP = 200_000;

const PRE =
  "bg-muted/50 ring-foreground/10 overflow-x-auto rounded-lg p-3 font-mono text-caption-md-regular leading-5 ring-1";

type Shot = { name: string; url: string };

/**
 * A coding task's deliverable on the device: the agent's report, its diff and its
 * screenshots. The archive is fetched from the artifact's own download link and read here; each
 * screenshot becomes a blob URL, typed as a PNG, for as long as the view lives. A fetch or a parse
 * that fails, or an archive too big to hold, falls to the DownloadCard.
 */
export default function ChangeView({ artifact, download }: RendererProps) {
  const [state, setState] = useState<{ change?: ChangeArchive; shots?: Shot[]; failed?: boolean }>(
    {},
  );

  useEffect(() => {
    if (artifact.bytes > MAX_ARCHIVE_BYTES) {
      setState({ failed: true });
      return;
    }
    let cancelled = false;
    let shots: Shot[] = [];
    fetch(download.url)
      .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(String(r.status)))))
      .then((buffer) => {
        if (cancelled) return;
        const change = parseChange(buffer);
        shots = change.screenshots.map(({ name, bytes }) => ({
          name,
          url: URL.createObjectURL(new Blob([bytes], { type: "image/png" })),
        }));
        setState({ change, shots });
      })
      .catch(() => !cancelled && setState({ failed: true }));
    return () => {
      cancelled = true;
      shots.forEach((shot) => URL.revokeObjectURL(shot.url));
    };
  }, [artifact.bytes, download.url]);

  if (state.failed) return <DownloadCard artifact={artifact} download={download} degraded />;
  if (!state.change) return <LoadingState label="Loading the change…" />;
  return <ChangeBody change={state.change} screenshots={state.shots ?? []} />;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-body-sm-regular font-medium">{title}</h3>
      {children}
    </section>
  );
}

function Absent({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground text-caption-md-regular">{children}</p>;
}

/** The presentational half, exported for render pins - no fetch, no effects. The report goes
 *  through the safe markdown path with `inert`, so nothing in it links to a file or loads an
 *  image; the diff and the test output are text, never markup. */
export function ChangeBody({
  change,
  screenshots,
}: {
  change: ChangeArchive;
  screenshots: Shot[];
}) {
  const { report, diff, testOutput, notes } = change;
  return (
    <div className="space-y-6">
      <Section title="Report">
        {report === null ? (
          <Absent>This change has no report.</Absent>
        ) : (
          <div className="ring-foreground/10 overflow-hidden rounded-lg p-4 ring-1">
            <div className="max-w-3xl">
              <Markdown inert>{report}</Markdown>
            </div>
          </div>
        )}
      </Section>

      <Section title="Diff">
        {diff === null ? (
          <Absent>This change has no patch. The report lists the files.</Absent>
        ) : diff.length > HIGHLIGHT_CAP ? (
          <pre className={PRE}>{diff}</pre>
        ) : (
          <Suspense fallback={<pre className={PRE}>{diff}</pre>}>
            <LazyAnswerCodeBlock code={diff} language="diff" />
          </Suspense>
        )}
      </Section>

      {testOutput === null ? (
        <Absent>This change has no test output.</Absent>
      ) : (
        <QuietDisclosure label="Test output" defaultOpen>
          <pre className={PRE}>{testOutput}</pre>
        </QuietDisclosure>
      )}

      {screenshots.length > 0 && (
        <Section title="Screenshots">
          <ul className="grid gap-3 sm:grid-cols-2">
            {screenshots.map((shot) => (
              <li key={shot.name}>
                <figure className="space-y-1">
                  <img
                    src={shot.url}
                    alt={shot.name}
                    loading="lazy"
                    className="ring-foreground/10 w-full rounded-lg ring-1"
                  />
                  <figcaption className="text-muted-foreground truncate text-caption-md-regular">
                    {shot.name}
                  </figcaption>
                </figure>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {notes.map((note) => (
        <p key={note} className="text-muted-foreground text-caption-md-regular">
          {note}
        </p>
      ))}
    </div>
  );
}
