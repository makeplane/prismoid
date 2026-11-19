/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * EpilogueBlock - the end of a turn as ONE quiet block (three stacked markers - green
 * Delivered, red Verification, Remembered - read as a traffic light; this reads as an ending).
 * What the check said sits above the delivery facts, and a rule down the side ties the two:
 * amber when the receipt asks for care, green when it is clean (epilogue-verdict.ts decides; the red verdict lives on the artifact badge, where you act on
 * it). The remembered items are elaborated in full - a count is not a memory - folded behind
 * the tree's own toggle grammar. Verification and memory frames fold INTO the item as they
 * arrive (`epilogueId` in the reducer); this file only renders.
 */

// Base UI's Collapsible, not Propel's: see kit/disclosure.tsx.
import { Collapsible } from "@base-ui/react/collapsible";
import { Ban, Brain, CheckCircle2, CircleSlash } from "lucide-react";
import { DisclosureChevron } from "~/components/kit";
import {
  PreviewCard,
  PreviewCardBody,
  PreviewCardContent,
  PreviewCardTrigger,
} from "@makeplane/propel/components/preview-card";
import { cn } from "~/lib/utils";
import {
  formatDurationMs,
  formatReceiptTokens,
  formatReceiptTokenTotal,
  formatTokens,
} from "~/lib/time";
import { routeLabel, type FeedItem } from "~/lib/task-view";
import { formatUsd } from "~/lib/format";
import { epilogueVerdict, type EpilogueVerdict, type VerdictTone } from "./epilogue-verdict";
import { TEXT_COL_PAD } from "./progress-copy";

type EpilogueItem = Extract<FeedItem, { kind: "epilogue" }>;
type Receipt = NonNullable<EpilogueItem["receipt"]>;

/** How many x-ray rows a hover card shows before the "+N more" line - a render clamp
 *  only (the tree grammar's law): the reducer keeps everything. */
const XRAY_CAP = 16;

/** The steps x-ray, one formatted line per STEP group: what ran, who ran it, how long,
 *  what it read/wrote. Pure - the hover card renders, this builds (portal content never
 *  reaches static markup, so the pins hold here). */
export function receiptStepRows(
  receipt: Receipt,
): { title: string; agent: string | null; meta: string; offNominal: string | null }[] {
  return (receipt.rows ?? [])
    .filter((r) => r.kind === "step")
    .map((r) => ({
      title: r.title || "(untitled step)",
      agent: r.agent ?? null,
      meta: [
        r.durationMs != null ? formatDurationMs(r.durationMs) : null,
        r.tokens != null || r.cachedTokens != null
          ? formatTokens(r.tokens ?? 0, r.cachedTokens)
          : null,
      ]
        .filter(Boolean)
        .join(" · "),
      // A skip or an issue is part of the story - named, never absorbed (the root
      // line's own law). Done stays quiet: the turn delivered.
      offNominal: r.state === "skipped" ? "skipped" : r.state === "escalated" ? "issue" : null,
    }));
}

/** The cast rollup: each agent with how many steps it ran and its token total (the
 *  plan group's planner included when one drafted the plan - same cast law as the tree headers). */
export function receiptCast(receipt: Receipt): { agent: string; steps: number; meta: string }[] {
  const by = new Map<string, { steps: number; tokens: number; cached: number; plan: boolean }>();
  for (const r of receipt.rows ?? []) {
    if (!r.agent) continue;
    const c = by.get(r.agent) ?? { steps: 0, tokens: 0, cached: 0, plan: false };
    if (r.kind === "step") c.steps += 1;
    else c.plan = true;
    c.tokens += r.tokens ?? 0;
    c.cached += r.cachedTokens ?? 0;
    by.set(r.agent, c);
  }
  return [...by.entries()].map(([agent, c]) => {
    const parts = [
      c.steps > 0 ? `${c.steps} step${c.steps === 1 ? "" : "s"}` : null,
      c.tokens > 0 || c.cached > 0 ? formatTokens(c.tokens, c.cached) : null,
    ].filter(Boolean);
    return {
      agent,
      steps: c.steps,
      meta: parts.length ? parts.join(" · ") : c.plan ? "planning" : "",
    };
  });
}

/** The delivery marker's word: a turn whose answer did only part of the work, or declined, or was
 *  stopped, never says "Delivered". */
function deliveryWord(item: EpilogueItem): string {
  if (item.stopped) return stoppedWord(item.stopped);
  if (item.couldNot) return "Couldn't do it";
  return item.partial ? "Partly done" : "Delivered";
}

function stoppedWord(stopped: NonNullable<EpilogueItem["stopped"]>): string {
  const why = stopped.reason ? `: ${stopped.reason}` : "";
  return stopped.by ? `Stopped by ${stopped.by}${why}` : `Stopped${why}`;
}

export function EpilogueBlock({
  item,
  onOpenArtifact,
}: {
  item: EpilogueItem;
  /** Opens the artifact the receipt is on - the failed caveat's link. */
  onOpenArtifact?: (artifactId: string) => void;
}) {
  const verdict = epilogueVerdict(item);
  return (
    <div className="text-muted-foreground space-y-1.5 pt-1 text-body-sm-regular">
      {/* The verdict and the delivery facts are ONE unit, tied by a rule down the side whose
          colour is the verdict: amber asks for care, green is a clean receipt. A turn with no
          receipt wears no rule. */}
      <div
        className={cn("space-y-1.5", verdict && "border-l-2 pl-3", verdict && RULE[verdict.tone])}
      >
        {verdict && <VerdictLine item={item} verdict={verdict} onOpenArtifact={onOpenArtifact} />}
        <DeliveryLine item={item} />
      </div>
      {/* A stopped turn's account: what it finished, with its files, and what it never did. */}
      <StopAccount item={item} />
      {/* A DIFFERENT concept, its own line: what the machine will remember. */}
      <Remembered items={item.remembered} />
    </div>
  );
}

/** The delivery sentence. "Delivered in 33s" reads as one fact; any other ending keeps its own
 *  words and the time follows as a plain figure. The steps and agents figures are X-RAYS (the
 *  hover ask, round seven): the numbers open into the actual story - which steps, whose hands -
 *  without scrolling back to the trees. Tokens open into fresh and cached. */
function DeliveryLine({ item }: { item: EpilogueItem }) {
  const r = item.receipt;
  const word = deliveryWord(item);
  const timed = word === "Delivered" && r?.durationMs != null;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <EndIcon item={item} />
      <span>
        <span className="text-foreground">{word}</span>
        {timed && ` in ${formatDurationMs(r.durationMs!)}`}
      </span>
      {/* The route (the disclosure): which door took the turn, in the reader's
          words - "answered" for a breath, "planned" for work, "answered, then planned"
          for the hatch; the door's own reason opens on hover. */}
      {item.route && (
        <ReceiptBit label={routeLabel(item.route)}>
          {item.route.why ? (
            <p className="max-w-64 text-caption-md-regular">{item.route.why}</p>
          ) : undefined}
        </ReceiptBit>
      )}
      {r && <ReceiptFacts receipt={r} timed={timed} />}
      {item.checkUsd != null && <ReceiptBit label={`${formatUsd(item.checkUsd)} to check`} />}
    </div>
  );
}

/** The receipt's countable facts. `timed`: the duration already rides the delivery word. */
function ReceiptFacts({ receipt: r, timed }: { receipt: Receipt; timed: boolean }) {
  const tokens = formatReceiptTokenTotal(r);
  return (
    <>
      {r.steps > 0 && (
        <ReceiptBit label={`${r.steps} step${r.steps === 1 ? "" : "s"}`}>
          <StepsXray receipt={r} />
        </ReceiptBit>
      )}
      {r.agents > 1 && (
        <ReceiptBit label={`${r.agents} agents`}>
          <CastXray receipt={r} />
        </ReceiptBit>
      )}
      {!timed && r.durationMs != null && <ReceiptBit label={formatDurationMs(r.durationMs)} />}
      {tokens && (
        <ReceiptBit label={tokens}>
          <p className="tabular-nums">{formatReceiptTokens(r)}</p>
        </ReceiptBit>
      )}
    </>
  );
}

/** What the machine will remember, folded behind the same toggle grammar as the tree above. */
function Remembered({ items }: { items: EpilogueItem["remembered"] }) {
  if (items.length === 0) return null;
  return (
    <Collapsible.Root>
      <Collapsible.Trigger className="hover:text-foreground group flex w-full items-center gap-2 text-left transition-colors">
        <Brain className="size-3.5 shrink-0" />
        <span className="min-w-0 truncate">
          Remembered {items.length} {items.length === 1 ? "thing" : "things"}
        </span>
        <DisclosureChevron />
      </Collapsible.Trigger>
      <Collapsible.Panel className={cn("pt-1.5", TEXT_COL_PAD)}>
        <ul className="space-y-1">
          {items.map((m) => (
            <li key={m.text} className="text-muted-foreground/80 text-caption-md-regular">
              {m.kind && <span className="text-muted-foreground/50">{m.kind} · </span>}
              {m.text}
            </li>
          ))}
        </ul>
      </Collapsible.Panel>
    </Collapsible.Root>
  );
}

function EndIcon({ item }: { item: EpilogueItem }) {
  const Icon = item.stopped ? Ban : item.couldNot ? CircleSlash : CheckCircle2;
  return <Icon className="size-3.5 shrink-0" />;
}

const RULE: Record<VerdictTone, string> = {
  warning: "border-warning",
  success: "border-success",
};
const VERDICT_TEXT: Record<VerdictTone, string> = {
  warning: "text-warning",
  success: "text-success",
};

/** What the check said, above the facts it is about, with the way to the evidence. A failed
 *  receipt is a caveat (delivery did happen, and the receipt disagrees with the answer): said
 *  once, in plain words, as a note - never red, never a blocker. */
function VerdictLine({
  item,
  verdict,
  onOpenArtifact,
}: {
  item: EpilogueItem;
  verdict: EpilogueVerdict;
  onOpenArtifact?: (artifactId: string) => void;
}) {
  const artifactId = item.artifactId;
  return (
    <div
      className={cn("flex flex-wrap items-center gap-x-2.5 gap-y-1", VERDICT_TEXT[verdict.tone])}
      role={verdict.caveat ? "note" : undefined}
    >
      <span>{verdict.text}</span>
      {artifactId && onOpenArtifact && (
        <button
          type="button"
          onClick={() => onOpenArtifact(artifactId)}
          className="hover:text-foreground underline decoration-dotted underline-offset-3 transition-colors"
        >
          See the receipt
        </button>
      )}
    </div>
  );
}

/** A stopped turn's account as the server wrote it: a heading line, then one line per step. */
function StopAccount({ item }: { item: EpilogueItem }) {
  const [head, ...rows] = (item.stopped?.account ?? "").split("\n").filter((l) => l.trim());
  if (!head) return null;
  return (
    <div className={cn("space-y-0.5", TEXT_COL_PAD)}>
      <p className="text-foreground">{head}</p>
      {rows.length > 0 && (
        <ul className="space-y-0.5">
          {rows.map((row) => (
            <li key={row}>{row.replace(/^- /, "")}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** One receipt fact on the delivery sentence. With children it is a HOVER X-RAY: the
 *  dotted underline is the whole affordance (quiet - the settled line must not read as
 *  a button row), and the card opens fast (150ms - an x-ray, not a link preview). */
function ReceiptBit({ label, children }: { label: string; children?: React.ReactNode }) {
  const bit = <span className="tabular-nums">{label}</span>;
  return (
    <span className="flex items-center gap-x-2">
      <span className="text-muted-foreground/40">·</span>
      {children ? (
        <PreviewCard>
          <PreviewCardTrigger
            delay={150}
            closeDelay={100}
            render={
              <span className="decoration-muted-foreground/50 cursor-default underline decoration-dotted underline-offset-3" />
            }
          >
            {bit}
          </PreviewCardTrigger>
          <PreviewCardContent side="top">
            <PreviewCardBody>
              <div className="text-caption-md-regular/relaxed">{children}</div>
            </PreviewCardBody>
          </PreviewCardContent>
        </PreviewCard>
      ) : (
        bit
      )}
    </span>
  );
}

/** The steps behind the number: title · AGENT · time, one quiet row each, feed order.
 *  Skips and issues are named in amber-free muted text (the delivered line stays
 *  monochrome; the tree above carries the colored account). */
function StepsXray({ receipt }: { receipt: Receipt }) {
  const rows = receiptStepRows(receipt);
  const shown = rows.slice(0, XRAY_CAP);
  return (
    <div className="space-y-1">
      {shown.map((row, i) => (
        <div key={i} className="flex items-baseline gap-2">
          <span className="text-popover-foreground min-w-0 flex-1 truncate">
            {row.title}
            {row.offNominal && <span className="text-muted-foreground"> · {row.offNominal}</span>}
          </span>
          {row.agent && (
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-wide uppercase">
              {row.agent}
            </span>
          )}
          {row.meta && (
            <span className="text-muted-foreground shrink-0 text-right tabular-nums">
              {row.meta}
            </span>
          )}
        </div>
      ))}
      {rows.length > shown.length && (
        <p className="text-muted-foreground">+{rows.length - shown.length} more</p>
      )}
    </div>
  );
}

/** The cast behind the number: who worked, how much - a planner included when one drafted
 *  the plan (its work is the planning phase). */
function CastXray({ receipt }: { receipt: Receipt }) {
  return (
    <div className="space-y-1">
      {receiptCast(receipt).map((c) => (
        <div key={c.agent} className="flex items-baseline gap-2">
          <span className="text-popover-foreground min-w-0 flex-1 truncate font-mono text-[10px] tracking-wide uppercase">
            {c.agent}
          </span>
          <span className="text-muted-foreground shrink-0 tabular-nums">{c.meta}</span>
        </div>
      ))}
    </div>
  );
}
