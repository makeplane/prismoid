/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { failedCaveat } from "~/lib/verdict-copy";
import { Calendar, Download, FileText, Fingerprint, ShieldAlert, ShieldCheck } from "lucide-react";
import { Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import {
  CopyButton,
  DisclosureChevron,
  LoadingState,
  Markdown,
  MetaChip,
  PageHeader,
  Pill,
  VerdictBadge,
  artifactKindLabel,
  type Tone,
} from "~/components/kit";
import { DownloadCard } from "~/components/artifact/download-card";
import { RendererBoundary } from "~/components/artifact/renderers/renderer-boundary";
import { RevisionHistory } from "~/components/artifact/revision-history";
import {
  bareModel,
  checkSummary,
  isLongCheck,
  judgeCell,
  receiptSummary,
} from "~/components/artifact/receipt-copy";
import { rendererFor } from "~/components/artifact/renderers/registry";
import { useReceiptFold } from "~/hooks/use-receipt-fold";
import { lazy } from "react";

// The render-sidecar viewer (sheets, documents, decks, code, pages) -
// lazy: an artifact with a registered renderer never pays for it.
const PreviewView = lazy(() => import("~/components/artifact/renderers/preview-view"));
// The full-fidelity rendition viewer - wins over the extracted preview when present.
const RenditionView = lazy(() => import("~/components/artifact/renderers/rendition-view"));
import { Button } from "@makeplane/propel/components/button";
import { Badge } from "@makeplane/propel/components/badge";
import { Icon } from "@makeplane/propel/components/icon";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@makeplane/propel/components/table";
import { api, unwrap } from "~/lib/api/client";
import { formatBytes, formatDateTime } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";

type ArtifactOut = components["schemas"]["ArtifactOut"];
type VerificationOut = components["schemas"]["VerificationOut"];
type DownloadOut = components["schemas"]["DownloadOut"];

/**
 * The full artifact detail - provenance, a markdown preview, and the verification report. Self-fetching
 * by id so it works both as the /artifacts/:id route body and inside the chat's artifact side panel.
 * `hostActions`: the host pane's controls (full screen, close), rendered INTO the title row's
 * action cluster so every control shares one baseline - and kept visible while loading, so the
 * pane never traps the user without a close.
 * `showDownload` / `onDownloadInfo`: a host with a PINNED band (the task page's split pane)
 * renders Download itself - inside the title row the button rode the horizontal pan of wide
 * content and clipped off-window. The view still owns the ONE fetch; it reports
 * the link outward, always for the version currently VIEWED.
 * `chrome="bare"`: the host's band already names the artifact - skip the PageHeader block
 * (title + meta chips) entirely. Its height was what pushed an app-frame board past the
 * pane and minted a scroll-inside-a-scroll; the version picker, content, and receipt stay.
 */
/** Whether the reader may edit the content in place (document text, collection cells): only
 *  the lineage's head, and only for a reader allowed to write (`readOnly` is a Project viewer -
 *  the server's `_require_edit_rights` would 403 the save). Pure. */
export function editsAllowed(isHead: boolean, readOnly?: boolean): boolean {
  return isHead && !readOnly;
}

export function ArtifactView({
  artifactId,
  hostActions,
  showDownload = true,
  onDownloadInfo,
  onArtifactChanged,
  chrome = "full",
  readOnly = false,
  verdict,
}: {
  artifactId: string;
  hostActions?: ReactNode;
  showDownload?: boolean;
  onDownloadInfo?: (download: DownloadOut | null) => void;
  /** A save or restore minted a NEW lineage head - hosts that key on the open
   *  artifact's id (the task page's band) repoint here; viewing an old version does not fire. */
  onArtifactChanged?: (artifactId: string) => void;
  chrome?: "full" | "bare";
  /** The reader may not write (a Project viewer): no edit, no restore, no label. */
  readOnly?: boolean;
  /** The head's verdict as the host's live stream saw it. The receipt signs after publish, on its
   *  own queue, so it usually lands after the reader opened: a new verdict re-reads it here. */
  verdict?: string | null;
}) {
  // The id being VIEWED - moves along the lineage via the version picker or a save.
  const [currentId, setCurrentId] = useState(artifactId);
  useEffect(() => setCurrentId(artifactId), [artifactId]);
  // A NEW HEAD was minted (save / restore): view it AND tell the host - the reducer swaps the
  // shelf id in place on `artifact.revised`, so a host that keeps the old id blanks its band.
  function advance(id: string) {
    setCurrentId(id);
    onArtifactChanged?.(id);
  }

  // Latest-callback ref: reporting must not re-trigger the fetch effect on parent renders.
  const onDownloadInfoRef = useRef(onDownloadInfo);
  onDownloadInfoRef.current = onDownloadInfo;

  // The receipt's fold lives here so the failed-verification note above the content can open it.
  const receipt = useReceiptFold(currentId);

  const [state, setState] = useState<{
    artifact?: ArtifactOut;
    verification?: VerificationOut | null;
    download?: DownloadOut | null;
    revisions?: ArtifactOut[];
    loading: boolean;
    error?: string;
  }>({ loading: true });

  useEffect(() => {
    let cancelled = false;
    const path = { artifact_id: currentId };
    setState((s) => ({ ...s, loading: true }));
    Promise.all([
      unwrap(api.GET("/v1/artifacts/{artifact_id}", { params: { path } })),
      unwrap(
        api.GET("/v1/artifacts/{artifact_id}/verification", {
          params: { path },
        }),
      ).catch(() => null),
      unwrap(api.GET("/v1/artifacts/{artifact_id}/download", { params: { path } })).catch(
        () => null,
      ),
      unwrap(api.GET("/v1/artifacts/{artifact_id}/revisions", { params: { path } }))
        .then((page) => page.items)
        .catch(() => []),
    ])
      .then(([artifact, verification, download, revisions]) => {
        if (cancelled) return;
        setState({
          artifact,
          verification,
          download,
          revisions,
          loading: false,
        });
        onDownloadInfoRef.current?.(download ?? null);
      })
      .catch((e) => {
        if (!cancelled)
          setState({
            loading: false,
            error: e instanceof Error ? e.message : "Failed",
          });
      });
    return () => {
      cancelled = true;
    };
  }, [currentId]);

  // The verdict the open read already reflects, per artifact (the view is not remounted when the
  // host opens another): only a new one re-reads the receipt, and only for the head (an older
  // version's receipt is its own).
  const verdictRead = useRef({ id: artifactId, verdict });
  useEffect(() => {
    const read = verdictRead.current;
    if (read.id !== artifactId) {
      verdictRead.current = { id: artifactId, verdict }; // another artifact: its open read is the base
      return;
    }
    if (!verdict || verdict === read.verdict || currentId !== artifactId) return;
    verdictRead.current = { id: artifactId, verdict };
    let cancelled = false;
    unwrap(
      api.GET("/v1/artifacts/{artifact_id}/verification", {
        params: { path: { artifact_id: artifactId } },
      }),
    )
      .then((verification) => !cancelled && setState((s) => ({ ...s, verification })))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [verdict, currentId, artifactId]);

  if (state.loading) {
    return (
      <>
        {hostActions && <div className="flex justify-end gap-1.5">{hostActions}</div>}
        <LoadingState label="Loading artifact…" />
      </>
    );
  }
  if (state.error || !state.artifact) {
    return (
      <>
        {hostActions && <div className="flex justify-end gap-1.5">{hostActions}</div>}
        <p className="text-muted-foreground py-16 text-center text-body-sm-regular">
          Couldn't load this artifact.
        </p>
      </>
    );
  }

  const { artifact, verification, download, revisions } = state;
  // App-frame kinds fill the host's box (the host killed its own scroll); documents flow.
  const appFrame = rendererFor(artifact.render)?.frame === "app" && !!download;
  // Editing is a HEAD affordance: the server refuses a revise on any older
  // revision, so a renderer viewing one must not offer Save - editing v1 of a 3-deep
  // lineage otherwise 409s on the first save with a false "vN arrived while you were
  // editing" banner. Withholding the callback is the gate every editor already keys on.
  const isHead = !revisions || revisions.length === 0 || artifact.id === revisions[0].id;
  return (
    <div className={appFrame ? "flex h-full min-h-0 flex-col gap-4" : "space-y-6"}>
      {chrome === "full" && (
        <PageHeader
          as="h2"
          className="pb-0"
          title={artifact.title}
          description={
            <span className="flex flex-wrap items-center gap-3">
              <MetaChip icon={FileText}>{artifactKindLabel(artifact.type)}</MetaChip>
              {artifact.revision > 1 && <MetaChip>v{artifact.revision}</MetaChip>}
              {artifact.label && <MetaChip>{artifact.label}</MetaChip>}
              <MetaChip>{formatBytes(artifact.bytes)}</MetaChip>
              <MetaChip icon={Calendar}>{formatDateTime(artifact.created_at)}</MetaChip>
              <VerdictBadge verdict={artifact.verdict} />
            </span>
          }
          actions={
            ((download && showDownload) || hostActions) && (
              <span className="flex items-center gap-1.5">
                {download && showDownload && (
                  <Button
                    nativeButton={false}
                    render={<a href={download.url} download={download.filename} />}
                    variant="primary"
                    size="sm"
                    stretch="auto"
                    icon={<Icon icon={Download} />}
                    label="Download"
                  />
                )}
                {hostActions}
              </span>
            )
          }
        />
      )}

      {revisions && (
        <RevisionHistory
          revisions={revisions}
          viewedId={artifact.id}
          onView={setCurrentId}
          onAdvance={(a) => advance(a.id)}
          readOnly={readOnly}
        />
      )}

      <FailedNote verification={verification} onOpenReceipt={receipt.reveal} />

      <div className={appFrame ? "flex min-h-0 flex-1 flex-col" : undefined}>
        <ArtifactContent
          artifact={artifact}
          download={download ?? null}
          onArtifactChanged={editsAllowed(isHead, readOnly) ? advance : undefined}
          fill={appFrame}
        />
      </div>

      {verification ? (
        <VerificationDisclosure
          v={verification}
          frame={appFrame}
          open={receipt.open}
          onOpenChange={receipt.setOpen}
          containerRef={receipt.ref}
        />
      ) : (
        <p className="text-muted-foreground text-caption-md-regular">
          Not verified yet. The receipt shows here once the check finishes.
        </p>
      )}
    </div>
  );
}

/** The note above the content when the checks failed: delivery stands, the receipt disagrees. */
function FailedNote({
  verification,
  onOpenReceipt,
}: {
  verification?: VerificationOut | null;
  onOpenReceipt: () => void;
}) {
  if (verification?.verdict !== "fail") return null;
  return (
    <div
      role="note"
      className="text-muted-foreground flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 text-caption-md-regular"
    >
      <ShieldAlert className="text-warning size-3.5 shrink-0" />
      <span>{failedCaveat()}</span>
      <button
        type="button"
        onClick={onOpenReceipt}
        className="hover:text-foreground underline decoration-dotted underline-offset-3 transition-colors"
      >
        See the receipt
      </button>
    </div>
  );
}

/**
 * The verification, folded to one quiet line: the CONTENT is the page;
 * provenance is a receipt you can open. The line still tells the whole verdict story - verdict,
 * suite, cross-family, signed - so collapsing hides detail, never truth.
 */
function VerificationDisclosure({
  v,
  frame,
  open,
  onOpenChange,
  containerRef,
}: {
  v: VerificationOut;
  frame?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  containerRef: React.Ref<HTMLDivElement>;
}) {
  return (
    // In an app frame the receipt is the STATUS BAR: always visible below the lens, and when
    // opened it becomes a bounded bottom panel (its body scrolls; the lens above just shrinks)
    // - the page still never scrolls. In document flow it expands in place as before.
    <div
      ref={containerRef}
      className={`ring-foreground/10 overflow-hidden rounded-xl shadow-raised-100 ring-1 ${
        frame ? "flex max-h-[min(24rem,50%)] min-h-0 shrink-0 flex-col" : ""
      }`}
    >
      {/* The receipt row: the STAMP leads (the verdict is the news), the provenance story
          follows in muted, and the chevron trails as the quiet "there's more" - never an
          accordion widget with a control up front. The whole row is the click target. */}
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        data-panel-open={open || undefined}
        className="group hover:bg-muted/40 flex w-full shrink-0 items-center gap-2.5 px-4 py-2.5 text-left transition-[background-color] duration-150"
      >
        <VerdictBadge verdict={v.verdict} />
        <span className="text-muted-foreground min-w-0 flex-1 truncate text-caption-md-regular">
          {receiptSummary(v)}
        </span>
        <DisclosureChevron className="size-4" />
      </button>
      {open && (
        <div
          className={`border-t px-4 pt-4 pb-5 ${frame ? "scrollbar-thin min-h-0 overflow-y-auto" : ""}`}
        >
          <VerificationReport v={v} />
        </div>
      )}
    </div>
  );
}

/**
 * Renderer dispatch. The server's `artifact.render` hint picks the renderer; the shell
 * owns fetching, the boundary, and the DownloadCard floor - a renderer owns pixels only. The
 * ladder: registered renderer → fetch-failed/degraded card → DownloadCard. Mojibake is now
 * structurally impossible: <Markdown> is only reachable via the server's `markdown` hint.
 */
function ArtifactContent({
  artifact,
  download,
  onArtifactChanged,
  fill,
}: {
  artifact: ArtifactOut;
  download: DownloadOut | null;
  onArtifactChanged?: (artifactId: string) => void;
  fill?: boolean;
}) {
  const entry = rendererFor(artifact.render);
  const wantsText = entry?.source === "text" && download != null;
  const [state, setState] = useState<{
    text?: string;
    failed?: boolean;
    loading: boolean;
  }>({ loading: wantsText });

  const url = download?.url;
  useEffect(() => {
    if (!wantsText || !url) return;
    let cancelled = false;
    setState({ loading: true });
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((t) => !cancelled && setState({ text: t, loading: false }))
      .catch(() => !cancelled && setState({ failed: true, loading: false }));
    return () => {
      cancelled = true;
    };
  }, [wantsText, url]);

  if (!entry || !download) {
    // No renderer for these bytes - but a verify-frame sidecar may still show their content
    // honestly (computed sheet heads for an xlsx). The DownloadCard stays the floor beneath it.
    return (
      <div className="space-y-4">
        {/* Rendition and preview are INDEPENDENT best-effort legs: a rendition
            can exist when extraction failed or was too big - gate on either, never just one.
            No `download` prop here: the card below is already the floor. */}
        {(artifact.has_rendition || artifact.has_preview) && (
          <RendererBoundary fallback={null}>
            <Suspense fallback={<LoadingState label="Loading preview…" />}>
              {artifact.has_rendition ? (
                <RenditionView artifact={artifact} />
              ) : (
                <PreviewView artifactId={artifact.id} />
              )}
            </Suspense>
          </RendererBoundary>
        )}
        <DownloadCard artifact={artifact} download={download} />
      </div>
    );
  }
  if (entry.source === "text" && state.loading) {
    return <LoadingState label="Loading preview…" />;
  }
  if (entry.source === "text" && (state.failed || state.text == null)) {
    return <DownloadCard artifact={artifact} download={download} degraded />;
  }

  const Renderer = entry.Component;
  return (
    <RendererBoundary fallback={<DownloadCard artifact={artifact} download={download} degraded />}>
      <Suspense fallback={<LoadingState label="Loading preview…" />}>
        <Renderer
          artifact={artifact}
          download={download}
          text={state.text}
          onArtifactChanged={onArtifactChanged}
          fill={fill}
        />
      </Suspense>
    </RendererBoundary>
  );
}

const CHECK_TONE: Record<string, Tone> = {
  pass: "success",
  fail: "danger",
  skip: "muted",
  skipped: "muted",
};

function VerificationReport({ v }: { v: VerificationOut }) {
  const judge = judgeCell(v);
  return (
    <div className="space-y-4">
      {v.attempt > 1 && (
        <div className="flex flex-wrap items-center gap-3">
          <MetaChip icon={ShieldCheck}>attempt {v.attempt}</MetaChip>
        </div>
      )}

      <div className="grid gap-4 text-body-sm-regular sm:grid-cols-2">
        <div>
          <p className="text-muted-foreground text-caption-md-regular">Produced by</p>
          <p className="font-medium">{bareModel(v.producer_model)}</p>
          <p className="text-muted-foreground text-caption-md-regular">{v.producer_family}</p>
        </div>
        <div>
          <p className="text-muted-foreground text-caption-md-regular">Verified by</p>
          <p className="font-medium">{judge.name}</p>
          <p className="text-muted-foreground text-caption-md-regular">{judge.note}</p>
        </div>
      </div>

      {v.judge_rationale && (
        <div>
          <p className="text-muted-foreground mb-1 text-caption-md-regular">Judge rationale</p>
          {/* The judge writes markdown - render it, never its asterisks. */}
          <div className="text-body-sm-regular">
            <Markdown>{v.judge_rationale}</Markdown>
          </div>
        </div>
      )}

      {v.checks.length > 0 && (
        // whitespace-normal + wrap-anywhere inside the cells below: Propel's cell content is
        // nowrap (truncate), and these carry 64-char hashes and verbatim derivations - unwrapped,
        // the table's intrinsic width exploded and PANNED under the card border.
        // Receipts wrap; they never widen the pane. `isolate`: Propel's sticky z-20 header
        // cells must not paint over the shell's sticky z-10 topbar as the page scrolls.
        <div className="isolate">
          <Table variant="table">
            <TableHeader>
              <TableRow>
                <TableHead label="Check" />
                <TableHead label="Status" />
                <TableHead label="Expected" />
                <TableHead label="Actual" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {v.checks.map((c, i) => (
                <CheckRow key={`${c.name}-${i}`} c={c} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <div className="space-y-2 pt-2">
        <SigRow icon={Fingerprint} label={`signature (${v.algorithm})`} value={v.signature}>
          {v.signature_valid != null && (
            <Badge
              variant={v.signature_valid ? "neutral" : "danger"}
              size="xs"
              label={v.signature_valid ? "valid" : "invalid"}
            />
          )}
        </SigRow>
        <SigRow icon={Fingerprint} label="public key" value={v.public_key} />
      </div>
    </div>
  );
}

type CheckOut = components["schemas"]["CheckOut"];

/** One check. A check that is a wall of text (the grounding check lists every claim) shows its
 *  counts and folds the rest behind a toggle; short checks render as they always did. */
function CheckRow({ c }: { c: CheckOut }) {
  const long = isLongCheck(c);
  const [open, setOpen] = useState(false);
  const folded = long && !open;
  return (
    <TableRow>
      <TableCell>
        <div className="min-w-36 break-words whitespace-normal">
          <span className="font-medium">{c.name}</span>
          {c.detail && (
            <span className="text-muted-foreground block text-caption-md-regular">
              {folded ? checkSummary(c.detail) : c.detail}
            </span>
          )}
        </div>
      </TableCell>
      <TableCell>
        <Pill tone={CHECK_TONE[c.status] ?? "muted"} className="capitalize">
          {c.status}
        </Pill>
      </TableCell>
      <TableCell>
        <div className="text-muted-foreground min-w-28 [overflow-wrap:anywhere] text-caption-md-regular whitespace-normal">
          {c.expected ?? "-"}
        </div>
      </TableCell>
      <TableCell>
        <div className="text-muted-foreground min-w-28 [overflow-wrap:anywhere] text-caption-md-regular whitespace-normal">
          {folded ? "-" : (c.actual ?? "-")}
          {long && (
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
              className="hover:text-foreground mt-1 block underline decoration-dotted underline-offset-3 transition-colors"
            >
              {open ? "Hide details" : "Show details"}
            </button>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

function SigRow({
  icon: Icon,
  label,
  value,
  children,
}: {
  icon: typeof Fingerprint;
  label: string;
  value: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2">
      <Icon className="text-muted-foreground size-4 shrink-0" />
      <span className="text-muted-foreground w-32 shrink-0 text-caption-md-regular">{label}</span>
      <code className="bg-muted min-w-0 flex-1 truncate rounded-sm px-1.5 py-0.5 font-mono text-caption-md-regular">
        {value}
      </code>
      {children}
      <CopyButton value={value} />
    </div>
  );
}
