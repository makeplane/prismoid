/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import { DownloadCard } from "~/components/artifact/download-card";
import { LoadingState } from "~/components/kit";
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { withPageCsp } from "./page-csp";

type ArtifactOut = components["schemas"]["ArtifactOut"];
type DownloadOut = components["schemas"]["DownloadOut"];

/**
 * The FULL-FIDELITY view: LibreOffice's faithful render, made in the work frame at
 * harvest - a docx/pptx as a pixel-faithful PDF shown by the browser's own viewer, an xlsx as
 * its styled, recalculated HTML shown in the iframe cage. A native `.pdf` rides the same lane:
 * its own bytes ARE the rendition (`render:"pdf"` mounts this as the registered renderer).
 *
 * Failure has a floor: when this is the registered renderer there is no shell-provided
 * DownloadCard beneath it, so a failed fetch renders the degraded card itself (`download` is
 * threaded by the renderer dispatch). Mounted as the sidecar above an existing card floor
 * (artifact-view's preview branch), `download` is absent and failure degrades to null.
 */
export default function RenditionView({
  artifact,
  download,
}: {
  artifact: ArtifactOut;
  download?: DownloadOut | null;
}) {
  const [state, setState] = useState<{
    url?: string;
    html?: string;
    loading: boolean;
    failed?: boolean;
  }>({ loading: true });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true });
    unwrap(
      api.GET("/v1/artifacts/{artifact_id}/rendition", {
        params: { path: { artifact_id: artifact.id } },
      }),
    )
      .then(async (rendition) => {
        if (artifact.rendition_media === "text/html") {
          const raw = await fetch(rendition.url).then((r) =>
            r.ok ? r.text() : Promise.reject(new Error(String(r.status))),
          );
          if (!cancelled) setState({ html: raw, loading: false });
        } else if (!cancelled) {
          setState({ url: rendition.url, loading: false });
        }
      })
      .catch(() => !cancelled && setState({ loading: false, failed: true }));
    return () => {
      cancelled = true;
    };
  }, [artifact.id, artifact.rendition_media]);

  if (state.loading) return <LoadingState label="Loading document…" />;
  if (state.failed || (!state.url && !state.html)) {
    // The floor: a degraded card when we ARE the renderer, null when a card already sits below.
    return download ? <DownloadCard artifact={artifact} download={download} degraded /> : null;
  }
  return (
    <RenditionBody
      media={artifact.rendition_media ?? "application/pdf"}
      url={state.url}
      html={state.html}
      artifact={artifact}
      download={download}
    />
  );
}

/** The presentational half, exported for render pins - no fetch, no effects. */
export function RenditionBody({
  media,
  url,
  html,
  artifact,
  download,
}: {
  media: string;
  url?: string;
  html?: string;
  artifact?: ArtifactOut;
  download?: DownloadOut | null;
}) {
  if (media === "text/html" && html != null) {
    // The sheet cage: even stricter than the page cage - a rendition is a STATIC document, so
    // the sandbox grants NOTHING (no scripts at all), and the injected CSP still backstops it.
    return (
      <div className="space-y-2">
        <div
          className={
            /* token-ok: a rendered document is a white page in both themes */ "ring-foreground/10 overflow-hidden rounded-lg bg-white ring-1"
          }
        >
          <iframe
            title="Workbook (rendered, sandboxed)"
            srcDoc={withPageCsp(html)}
            sandbox=""
            referrerPolicy="no-referrer"
            loading="lazy"
            className="h-[75dvh] max-h-[56rem] w-full"
          />
        </div>
        <p className="text-muted-foreground text-caption-md-regular">
          Rendered in the sandbox with formulas recalculated · download for the workbook
        </p>
      </div>
    );
  }
  if (url == null) return null;
  return (
    <div className="space-y-2">
      <object
        data={url}
        type="application/pdf"
        aria-label="Document (rendered as PDF)"
        className="ring-foreground/10 h-[75dvh] max-h-[56rem] w-full rounded-lg ring-1"
      >
        {/* The literal fallback (DESIGN.md): a browser that will not embed PDFs gets the card. */}
        {artifact && download ? (
          <DownloadCard artifact={artifact} download={download} />
        ) : (
          <p className="text-muted-foreground p-4 text-body-sm-regular">
            Your browser will not embed PDFs - use the download below.
          </p>
        )}
      </object>
      <p className="text-muted-foreground text-caption-md-regular">
        Faithful render made in the sandbox · shown by your browser's PDF viewer · download for the
        original file
      </p>
    </div>
  );
}
