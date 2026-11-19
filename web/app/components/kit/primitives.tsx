/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Liveness } from "./liveness";
import { Check, Copy, Search } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Bubble, BubbleContent } from "@prismoid/blocks/bubble";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { Separator } from "@makeplane/propel/components/separator";
import { formatUsd } from "~/lib/format";
import { cn } from "~/lib/utils";
import { TONE_TINT, type Tone } from "./tone";

/**
 * Money - the ledger law reaches the UI: render the server's Decimal string, never do float math on a
 * cost client-side. `tabular-nums` so columns of figures line up.
 */
export function Money({
  usd,
  className,
  muted,
}: {
  usd: string | number | null | undefined;
  className?: string;
  muted?: boolean;
}) {
  const n = usd == null ? null : typeof usd === "number" ? usd : Number(usd);
  const valid = n != null && !Number.isNaN(n);
  // The exact figure rides the tooltip, for the ledger reader; the face is the readable one.
  return (
    <span
      className={cn("tabular-nums", muted && "text-muted-foreground", className)}
      title={valid ? `$${n.toFixed(6)}` : undefined}
    >
      {valid ? formatUsd(n) : "-"}
    </span>
  );
}

/** PageHeader - the title recipe for detail surfaces and drawers: title, optional
 *  description, optional actions on the right, optional `leading` block (an identity mark -
 *  the project's face) centered against the text. `as="h2"` when the same block renders
 *  inside a drawer/panel that already has an h1. */
export function PageHeader({
  title,
  description,
  actions,
  leading,
  as: Heading = "h1",
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  leading?: ReactNode;
  as?: "h1" | "h2";
  className?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-4 pb-2", className)}>
      {/* items-start: the leading mark tops out with the title's line box (the Perplexity
          alignment) - centering against a title+description block floats the mark between
          the lines, which reads as misaligned the moment the description wraps. */}
      <div className="flex min-w-0 items-start gap-3">
        {leading}
        <div className="space-y-1">
          <Heading className="font-heading text-h4-regular font-semibold tracking-tight">
            {title}
          </Heading>
          {description && (
            <p className="text-muted-foreground text-body-sm-regular">{description}</p>
          )}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Section - a titled block within a page. `headerClassName` insets the title/actions row
 *  when the children carry their own gutters (px-4 list rows), so the label sits flush
 *  with the content below it, not the container edge. */
export function Section({
  title,
  actions,
  children,
  className,
  headerClassName,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  headerClassName?: string;
}) {
  return (
    <section className={cn("space-y-3", className)}>
      {(title || actions) && (
        <div className={cn("flex items-center justify-between", headerClassName)}>
          {title && (
            <h2 className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
              {title}
            </h2>
          )}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

/** EmptyState - one shape for every "nothing here yet" so absence looks intentional, not broken.
 *  `compact` for inline absences inside lists/sections (short, no dashed frame ceremony). */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  compact,
  className,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed px-6 text-center",
        compact ? "py-6" : "py-14",
        className,
      )}
    >
      {Icon && !compact && (
        <div className="bg-muted text-muted-foreground rounded-full p-2.5">
          <Icon className="size-5" />
        </div>
      )}
      <div className="space-y-1">
        <p className="text-body-sm-regular font-medium">{title}</p>
        {description && (
          <p className="text-muted-foreground max-w-sm text-body-sm-regular">{description}</p>
        )}
      </div>
      {action}
    </div>
  );
}

/** Pill - THE status-tinted chip (12px). Classification labels use Propel's <Badge> (xs) instead;
 *  if it names a state or verdict it is a Pill, if it names a kind or role it is a Badge. */
export function Pill({
  tone = "muted",
  icon: Icon,
  className,
  children,
}: {
  tone?: Tone;
  icon?: React.ComponentType<{ className?: string }>;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-pill border px-2 py-0.5 text-caption-md-regular font-medium",
        TONE_TINT[tone],
        className,
      )}
    >
      {Icon && <Icon className="size-3.5" />}
      {children}
    </span>
  );
}

/** Rows - the list container. Ring hairline outside (same treatment as Card), divide inside -
 *  never `border` + `divide-y` hand-rolled, so lists and cards share one hairline system.
 *  `flush` keeps the divide system but drops the ring/radius, for nesting inside a surface
 *  that already draws the one ring (e.g. the collection ViewShell). */
export function Rows({
  as: Tag = "div",
  flush = false,
  className,
  children,
}: {
  as?: "div" | "ul";
  flush?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Tag
      className={cn(
        "divide-border divide-y",
        flush ? "" : "ring-foreground/10 overflow-hidden rounded-lg ring-1",
        className,
      )}
    >
      {children}
    </Tag>
  );
}

/** ToggleChip - the one segmented/selectable chip. Same recipe everywhere a choice toggles. */
export function ToggleChip({
  active,
  className,
  ...props
}: React.ComponentProps<"button"> & { active?: boolean }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-pill border px-3 py-1 text-caption-md-regular font-medium transition-colors",
        active ? "border-primary bg-primary text-primary-foreground" : "hover:bg-muted",
        className,
      )}
      {...props}
    />
  );
}

/** IconTile - the square icon well used in rows and cards. EmptyState keeps its round one. */
export function IconTile({
  icon: Icon,
  tone = "muted",
  className,
}: {
  icon: React.ComponentType<{ className?: string }>;
  tone?: "muted" | "primary";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-md",
        tone === "primary"
          ? "bg-primary text-primary-foreground"
          : "bg-muted text-muted-foreground",
        className,
      )}
    >
      <Icon className="size-4" />
    </div>
  );
}

/** LoadingState - one loading treatment. A spinner and a sentence, centered; nothing pulses. */
export function LoadingState({
  label = "Loading…",
  className,
}: {
  label?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "text-muted-foreground flex items-center justify-center gap-2 py-16 text-body-sm-regular",
        className,
      )}
    >
      <Liveness className="size-4" />
      {label}
    </div>
  );
}

/** LabeledDivider - a rule with words in it ("Follow-up"). The only sanctioned mid-content rule. */
export function LabeledDivider({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "text-muted-foreground flex items-center gap-3 text-caption-md-regular",
        className,
      )}
    >
      <div className="flex-1">
        <Separator orientation="horizontal" decorative={false} />
      </div>
      {children}
      <div className="flex-1">
        <Separator orientation="horizontal" decorative={false} />
      </div>
    </div>
  );
}

/** FormError - the one error box under forms. Renders nothing when there is nothing to say. */
export function FormError({ children, className }: { children?: ReactNode; className?: string }) {
  if (!children) return null;
  return (
    <p
      className={cn(
        "rounded-md border px-3 py-2 text-body-sm-regular",
        TONE_TINT.danger,
        className,
      )}
    >
      {children}
    </p>
  );
}

/** MetaChip - a small muted icon+label for provenance/meta lines. */
export function MetaChip({
  icon: Icon,
  children,
  className,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "text-muted-foreground inline-flex items-center gap-1.5 text-caption-md-regular",
        className,
      )}
    >
      {Icon && <Icon className="size-3.5" />}
      {children}
    </span>
  );
}

/** CopyButton - THE copy control (signatures, keys, ids, the draft cards' actions). Pass
 *  `value` for a plain writeText, or `onCopy` for a custom write (the dual-MIME drafts);
 *  the check latch is promise-gated - a rejected write never latches - and holds the one
 *  look: 1200ms, text-success. `label` is the aria-label, which Propel's IconButton also
 *  shows as its tooltip ("Copy email"). `className` lands on a wrapper span. A failed
 *  write stays quiet in the UI (the affordance never grows an excuse) but warns to the
 *  console so a dead clipboard is diagnosable. */
export function CopyButton({
  value,
  onCopy,
  label = "Copy",
  className,
}: {
  value?: string;
  onCopy?: () => Promise<unknown> | unknown;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <span className={cn("inline-flex", className)}>
      <IconButton
        variant="ghost"
        size="md"
        aria-label={label}
        icon={<Icon icon={copied ? <Check className="text-success" /> : Copy} />}
        onClick={() => {
          void Promise.resolve()
            .then(onCopy ?? (() => navigator.clipboard.writeText(value ?? "")))
            .then(() => {
              setCopied(true);
              // A re-click must restart the beat - the first click's pending timer would
              // otherwise flip the check back early.
              window.clearTimeout(timer.current);
              timer.current = window.setTimeout(() => setCopied(false), 1200);
            })
            .catch((err: unknown) => {
              console.warn("copy failed", err);
            });
        }}
      />
    </span>
  );
}

/** The one search box every list speaks - typed three times before it earned this spot.
 *  Debouncing belongs to the caller; this is only the look. */
export function SearchBox({
  value,
  onChange,
  placeholder,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  className?: string;
}) {
  return (
    <div className={cn("w-full min-w-0 max-w-sm", className)}>
      <InputGroup size="md">
        <Icon icon={Search} tint="placeholder" />
        <Input
          size="md"
          value={value}
          onChange={(e) => onChange(e.currentTarget.value)}
          placeholder={placeholder}
          aria-label={placeholder}
        />
      </InputGroup>
    </div>
  );
}

/** The keyset page's tail: nothing when the list is complete, one quiet button otherwise. */
export function LoadMore({
  hasMore,
  busy,
  onClick,
  className,
}: {
  hasMore: boolean;
  busy: boolean;
  onClick: () => void;
  className?: string;
}) {
  if (!hasMore) return null;
  return (
    <div className={cn("flex justify-center", className)}>
      <Button
        variant="secondary"
        size="sm"
        stretch="auto"
        onClick={onClick}
        disabled={busy}
        label={busy ? "Loading…" : "Load more"}
      />
    </div>
  );
}

/** UserBubble - the user's words wherever a conversation renders (task page turns, the inbox
 *  replay): the universally-known chat grammar - quiet secondary surface, right edge, as-typed
 *  pre-wrap text, never Markdown. Promoted on its second consumer. */
export function UserBubble({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <Bubble variant="secondary" align="end" className={cn("ml-auto", className)}>
      <BubbleContent className="px-3.5 py-2 text-body-sm-regular whitespace-pre-wrap">
        {children}
      </BubbleContent>
    </Bubble>
  );
}

/** Up to two initials from a display name - the avatar vocabulary, one definition. */
export function initials(name: string): string {
  return (
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase())
      .join("") || "?"
  );
}
