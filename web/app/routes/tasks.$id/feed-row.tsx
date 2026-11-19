/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  AlertTriangle,
  BadgeCheck,
  Brain,
  CheckCircle2,
  Cpu,
  FileText,
  GitBranch,
  HelpCircle,
  MessageCircleQuestion,
  Paperclip,
  RotateCcw,
  ShieldAlert,
  SquareChevronRight,
  Wrench,
  X,
} from "lucide-react";
import { memo, useState } from "react";
import { Avatar } from "@makeplane/propel/components/avatar";
import { Button } from "@makeplane/propel/components/button";
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogMain,
  DialogTitle,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Marker, MarkerContent, MarkerIcon } from "@prismoid/blocks/marker";
import { ArtifactCard } from "~/components/artifact/artifact-card";
import {
  Markdown,
  Pill,
  TONE_CARD,
  TONE_TEXT,
  UserBubble,
  artifactIcon,
  initials,
} from "~/components/kit";
import { formatBytes } from "~/lib/format";
import { useSmoothText } from "~/lib/smooth-text";
import type {
  ArtifactView as ArtifactData,
  FeedItem,
  MarkerGlyph,
  SteerAck,
} from "~/lib/task-view";
import { cn } from "~/lib/utils";
import { EpilogueBlock } from "./epilogue";
import { ProgressTree } from "./progress-tree";

const GLYPH: Record<MarkerGlyph, typeof GitBranch> = {
  plan: GitBranch,
  step: SquareChevronRight,
  retry: RotateCcw,
  tool: Wrench,
  artifact: FileText,
  verified: BadgeCheck,
  failed: ShieldAlert,
  escalation: AlertTriangle,
  gate: HelpCircle,
  delivered: CheckCircle2,
  clarify: MessageCircleQuestion,
  memory: Brain,
  machine: Cpu,
};

/** An attachment chip on the opening turn: an image opens in a lightbox (the presigned
 *  url IS the auth - same public-endpoint signing as artifact downloads), anything else
 *  downloads. A chip that answered a click with nothing was a real complaint. */
function InputChip({
  input,
}: {
  input: { filename: string; bytes: number; media_type: string; url: string };
}) {
  const [open, setOpen] = useState(false);
  const image = input.media_type.startsWith("image/") && !!input.url;
  const chip = (
    <>
      <Paperclip className="size-3.5 shrink-0" />
      <span className="max-w-48 truncate">{input.filename}</span>
      <span className="text-muted-foreground/70 tabular-nums">{formatBytes(input.bytes)}</span>
    </>
  );
  const chipClass =
    "text-muted-foreground bg-muted hover:text-foreground flex items-center gap-1.5 rounded-pill px-2.5 py-1 text-caption-md-regular transition-colors";
  if (!image) {
    return input.url ? (
      <a href={input.url} download={input.filename} className={chipClass}>
        {chip}
      </a>
    ) : (
      <span className={chipClass}>{chip}</span>
    );
  }
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className={chipClass}>
        {chip}
      </button>
      {open && (
        <Dialog open onOpenChange={(o) => !o && setOpen(false)}>
          <DialogContent size="xl">
            <DialogMain>
              <div className="sr-only">
                <DialogTitle>{input.filename}</DialogTitle>
              </div>
              <DialogBody>
                {/* Capped under the popup's own ceiling (100dvh - 10rem) less its gutters and
                    the caption, so the image never scrolls. */}
                <img
                  src={input.url}
                  alt={input.filename}
                  className="mx-auto max-h-[calc(100dvh-16rem)] w-auto rounded-lg object-contain"
                />
                <div className="text-muted-foreground flex items-baseline justify-between gap-3 px-1 pt-2 text-caption-md-regular">
                  <span className="min-w-0 truncate">{input.filename}</span>
                  <span className="tabular-nums">{formatBytes(input.bytes)}</span>
                </div>
              </DialogBody>
            </DialogMain>
            <DialogCloseGroup>
              <IconButton
                variant="ghost"
                size="xs"
                aria-label="Close"
                icon={<Icon icon={X} />}
                render={<DialogClose />}
              />
            </DialogCloseGroup>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

/** A stable empty default - a fresh `{}` per render would pierce FeedRow's memo. */
const NONE_SUPERSEDED: Readonly<Record<string, string>> = {};

/** A superseded artifact's line: whose steer's re-run replaced it on a shared task;
 *  on a personal task the only one steering is you, so it names no one (the pause copy's rule).
 *  Undefined when the artifact was not superseded. */
function supersededLine(
  superseded: Readonly<Record<string, string>>,
  artifactId: string,
  named?: boolean,
): string | undefined {
  if (!Object.hasOwn(superseded, artifactId)) return undefined;
  return named ? `Superseded by ${superseded[artifactId]}'s steer` : "Superseded by a steer";
}

/** Retract, latched while its POST is in flight: a second click must not send a second
 *  retract (the first may already have won; the frame redraws the line either way). */
function RetractButton({
  commandId,
  onRetract,
}: {
  commandId: string;
  onRetract: (commandId: string) => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      size="xs"
      variant="ghost"
      stretch="auto"
      label="Retract"
      disabled={busy}
      onClick={() => {
        if (busy) return;
        setBusy(true);
        void Promise.resolve(onRetract(commandId)).finally(() => setBusy(false));
      }}
    />
  );
}

/** The line under a steer's bubble: an ACCEPTED steer that cannot apply yet says
 *  what it waits on - the resume, or the open question's answer. */
function steerAckLine(
  ack: SteerAck,
  waits: { taskPaused?: boolean; questionOpen?: boolean } = {},
): string {
  if (ack.state !== "accepted") return ack.line;
  if (waits.taskPaused) return "Accepted · applies when the task resumes";
  if (waits.questionOpen) return "Accepted · applies after the question is answered";
  return ack.line;
}

/** The step's own evidence for why it gave up, one bullet per line; nothing to say, nothing drawn. */
function EscalationEvidence({ lines }: { lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <div className="text-muted-foreground space-y-1 text-caption-md-regular">
      <p className="font-medium">What happened</p>
      <Markdown>{lines.map((line) => `- ${line}`).join("\n")}</Markdown>
    </div>
  );
}

/** One transcript row. Memoized: a streamed delta re-renders only the live segment - every
 *  settled row (hundreds on a long task, each a markdown parse) keeps its tree.
 *  route.tsx re-exports it for the render pin in route.test.tsx. */
export const FeedRow = memo(function FeedRow({
  item,
  artifacts,
  onOpenArtifact,
  taskInputs,
  taskStatus,
  onStartProposal,
  onDeclineProposal,
  showAuthor,
  taskPaused,
  questionOpen,
  onRetract,
  myId,
  superseded = NONE_SUPERSEDED,
}: {
  item: FeedItem;
  artifacts: ArtifactData[];
  onOpenArtifact: (id: string) => void;
  /** THIS task's attachments - hung under its opening turn so a follow-up's image is
   *  visible in the prompt, the same chips the composer showed before send. */
  taskInputs?: {
    filename: string;
    bytes: number;
    media_type: string;
    url: string;
  }[];
  /** The task's status - the tree's root line tells the truth while parked. */
  taskStatus?: string;
  /** The proposal card's two verbs: Start is a follow-up with the deliverable
   *  dial set; Not now is local. Absent on the ancestor feed (a fork's parent is history). */
  onStartProposal?: (goal: string, deliverable: string | null) => Promise<void>;
  onDeclineProposal?: (id: string) => void;
  /** A shared task: a user turn wears its author's name above the bubble.
   *  Off on a personal task, where every turn is yours and the name would only be noise. */
  showAuthor?: boolean;
  /** The task is paused / a question is open: an ACCEPTED steer waits for either to lift,
   *  and its line says which. Passed to steer rows only. */
  taskPaused?: boolean;
  questionOpen?: boolean;
  /** Retract an unapplied steer - offered to its author only (absent for viewers). */
  onRetract?: (commandId: string) => Promise<void> | void;
  /** Who is looking: the author of an accepted steer may retract it. */
  myId?: string;
  /** Artifacts a steer's re-run superseded, by whose steer: greyed and labelled,
   *  never the delivered card. Stable (the view's own record) so the memo holds. */
  superseded?: Readonly<Record<string, string>>;
}) {
  // Paced reveal for the still-streaming segment (settled text passes through untouched).
  // Called before the early returns - hooks are unconditional.
  const streamed = useSmoothText(
    item.kind === "segment" ? (item.markdown ?? "") : "",
    item.kind === "segment" && item.live,
  );
  if (item.kind === "user") {
    // Your turn - ask, steer, follow-up - reads as "you said this" at a glance. (The minted
    // title in the breadcrumb/sidebar names the task now; ask-as-headline predated it.)
    const inputs = item.opening ? (taskInputs ?? []) : [];
    // No author = no name line: the optimistic paint (that row is yours) and any turn whose
    // event named no one.
    const who = showAuthor ? item.author : undefined;
    return (
      <div className="space-y-1.5">
        {who && (
          <p className="text-muted-foreground flex items-center justify-end gap-1.5 text-caption-md-regular">
            {/* no alt: the name beside it says who, so the face is decoration (aria-hidden) */}
            <Avatar size="sm" fallback={initials(who.name)} />
            <span className="min-w-0 truncate">
              {`${who.name} ${item.steered ? "steered" : "asked"}`}
            </span>
          </p>
        )}
        {/* pending = the optimistic paint, quietly translucent until the durable
            task.continued frame reconciles it */}
        <UserBubble className={item.pending ? "opacity-70" : undefined}>{item.text}</UserBubble>
        {/* where the steer stands - accepted, applied, rejected, retracted */}
        {item.steered && item.ack && (
          <div className="flex items-center justify-end gap-1.5">
            <p className="text-muted-foreground text-right text-caption-md-regular" role="status">
              {steerAckLine(item.ack, { taskPaused, questionOpen })}
            </p>
            {onRetract &&
              item.commandId &&
              item.ack.state === "accepted" &&
              myId != null &&
              item.author?.id === myId && (
                <RetractButton commandId={item.commandId} onRetract={onRetract} />
              )}
          </div>
        )}
        {inputs.length > 0 && (
          <div className="flex flex-wrap justify-end gap-1.5">
            {inputs.map((input, i) => (
              // index-qualified: two attachments can share a filename
              <InputChip key={`${input.filename}-${i}`} input={input} />
            ))}
          </div>
        )}
      </div>
    );
  }
  if (item.kind === "control") {
    // A person's action on the task - "Ada paused", "Bob resumed": one muted
    // centered line, the record's quietest voice.
    return (
      <p className="text-muted-foreground flex justify-center text-center text-caption-md-regular">
        {item.text}
      </p>
    );
  }
  if (item.kind === "qa") {
    // The answered batch, kept the way the reference reads: question in medium ink,
    // the answer quiet beneath it - one card for the whole round.
    return (
      <div className="ring-foreground/10 bg-card space-y-3 rounded-xl p-4 shadow-raised-100 ring-1">
        {item.items.map((qa) => (
          <div key={qa.prompt} className="space-y-0.5">
            <p className="text-body-sm-regular font-medium">{qa.prompt}</p>
            <p className="text-muted-foreground text-body-sm-regular">{qa.answer}</p>
          </div>
        ))}
      </div>
    );
  }
  if (item.kind === "artifact") {
    return (
      <ArtifactCard
        title={item.title}
        artifact={artifacts.find((a) => a.id === item.artifactId)}
        onOpen={() => onOpenArtifact(item.artifactId)}
        superseded={supersededLine(superseded, item.artifactId, showAuthor)}
      />
    );
  }
  if (item.kind === "epilogue") {
    // The end of the turn - one quiet sentence-shaped block (epilogue.tsx owns every
    // visual, the way progress-tree.tsx owns the tree's).
    return <EpilogueBlock item={item} onOpenArtifact={onOpenArtifact} />;
  }
  if (item.kind === "execution") {
    // The turn's execution story - the progress tree (progress-tree.tsx owns every
    // visual; this row only passes the item through the memo boundary).
    return <ProgressTree item={item} status={taskStatus} />;
  }
  if (item.kind === "escalation") {
    // A step's own account of why it gave up - a card, because the reason deserves more than
    // a clipped marker line (the old one ended at "The last, because:" and showed nothing).
    return <EscalationCard item={item} />;
  }
  if (item.kind === "proposal") {
    // The answerer's soft hatch: the bot-chat card's shape, on the task thread.
    // Nothing ran - the card says so, and the person decides.
    return (
      <div className="bg-card ring-foreground/10 space-y-2 rounded-xl p-4 shadow-raised-100 ring-1 text-body-sm-regular">
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground text-caption-md-regular">Proposed task</span>
          {item.deliverable && <Pill tone="muted">{item.deliverable}</Pill>}
          {!item.open && <Pill tone="muted">not started</Pill>}
        </div>
        <p>{item.goal}</p>
        {item.why && <p className="text-muted-foreground text-caption-md-regular">{item.why}</p>}
        {item.open && onStartProposal && (
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="primary"
              stretch="auto"
              label="Start"
              onClick={() => void onStartProposal(item.goal, item.deliverable)}
            />
            <Button
              size="sm"
              variant="ghost"
              stretch="auto"
              label="Not now"
              onClick={() => onDeclineProposal?.(item.id)}
            />
          </div>
        )}
      </div>
    );
  }
  if (item.kind === "marker") {
    const Icon = GLYPH[item.glyph];
    // Tone rides the ICON only; the words are always ink (amber text read
    // as shouting on routine machinery - the epilogue's icon-accent grammar is the law
    // everywhere now).
    return (
      <Marker>
        <MarkerIcon className={TONE_TEXT[item.tone]}>
          <Icon />
        </MarkerIcon>
        <MarkerContent>{item.text}</MarkerContent>
      </Marker>
    );
  }
  if (item.kind === "working-artifacts") {
    // Working artifacts as a CHIP ROW (round six): sections, finals, outlines - honest
    // and clickable, never full-card peers of the deliverable. They land right under
    // the phase tree that made them, so the transcript reads work → output.
    return (
      <div className="flex flex-wrap items-center gap-1.5">
        {item.items.map((c) => {
          const ChipIcon = artifactIcon(c.type);
          const line = supersededLine(superseded, c.artifactId, showAuthor);
          return (
            <button
              key={c.artifactId}
              type="button"
              onClick={() => onOpenArtifact(c.artifactId)}
              className={cn(
                "ring-foreground/10 hover:ring-foreground/25 bg-card flex max-w-64 items-center gap-1.5 rounded-md px-2 py-1 text-left text-caption-md-regular ring-1 transition-shadow",
                line && "opacity-60",
              )}
              title={line ? `${c.title} · ${line}` : c.title}
            >
              <ChipIcon className="text-muted-foreground size-3.5 shrink-0" />
              <span className="min-w-0 truncate">{c.title}</span>
            </button>
          );
        })}
      </div>
    );
  }
  // assistant message segment
  const pouring = item.kind === "segment" && item.live && streamed.trim().length > 0;
  return (
    // stream-tail: the pulsing cursor after the last character while the segment is live -
    // the answer itself must breathe between bursts, not only the ticker up top.
    <div className={cn("text-body-sm-regular", pouring && "stream-tail")}>
      {item.markdown ? (
        // live: block-memoized parse + tail healing + streaming block variants.
        <Markdown live={item.kind === "segment" && item.live === true} breaks>
          {streamed}
        </Markdown>
      ) : (
        <span className="text-muted-foreground shimmer">…</span>
      )}
    </div>
  );
});

/** A step's escalation card: red for a fault, neutral when the task then failed as an honest could-not. */
function EscalationCard({ item }: { item: Extract<FeedItem, { kind: "escalation" }> }) {
  return (
    <div
      className={cn(
        TONE_CARD[item.couldNot ? "muted" : "danger"],
        "space-y-2 rounded-xl border p-4",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-2 text-body-sm-regular font-medium",
          item.couldNot ? "text-foreground" : "text-destructive",
        )}
      >
        <AlertTriangle className="size-3.5 shrink-0" />
        <span>{item.headline}</span>
      </div>
      {/* The diagnosis is agent-authored prose - inline code and emphasis in it are
          real signal, so it renders as markdown, not a flat <p>. */}
      <Markdown>{item.reason}</Markdown>
      <EscalationEvidence lines={item.evidence} />
      {item.tries.length > 0 && (
        <div className="text-muted-foreground space-y-1 text-caption-md-regular">
          <p className="font-medium">What to try</p>
          <ul className="list-disc space-y-0.5 pl-4">
            {item.tries.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
