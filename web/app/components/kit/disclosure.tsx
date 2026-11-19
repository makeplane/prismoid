/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Base UI's Collapsible, not Propel's (ADR-074): Propel's panel animates height and clips its
// overflow (callers' rings and bleeds), and its ready-made trigger brings its own padding and type.
import { Collapsible } from "@base-ui/react/collapsible";
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "~/lib/utils";

/** DisclosureChevron - the rotating chevron every disclosure trigger wears (the
 *  `group-data-[panel-open]` hook is Base UI Collapsible's). One idiom that five
 *  surfaces used to retype: pass `className` for size ("size-3") or to inherit the
 *  trigger's color ("text-inherit" - needed when the trigger hover-shifts its ink). */
export function DisclosureChevron({ className }: { className?: string }) {
  return (
    <ChevronRight
      className={cn(
        "text-muted-foreground/60 size-3.5 shrink-0 transition-transform group-data-[panel-open]:rotate-90",
        className,
      )}
    />
  );
}

/** QuietDisclosure - the house recipe for a low-key expandable line (promoted to kit from
 *  the rail's "Earlier plans" row when the right side grew more quiet-expandable surfaces;
 *  the tree and the epilogue hand-roll richer triggers but share `DisclosureChevron`).
 *  A muted label with a small leading chevron that rotates on open; the panel indents
 *  under it. No height animation - the chevron transform is the only motion (DESIGN.md:
 *  never transition layout). Collapse state is the caller's (controlled) or Base UI's
 *  (uncontrolled via defaultOpen); never persisted. */
export function QuietDisclosure({
  label,
  count,
  open,
  defaultOpen = false,
  onOpenChange,
  triggerClassName,
  panelClassName,
  children,
}: {
  label: ReactNode;
  /** Quiet tabular count suffix - "· 3". */
  count?: number;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  triggerClassName?: string;
  panelClassName?: string;
  children: ReactNode;
}) {
  return (
    <Collapsible.Root open={open} defaultOpen={defaultOpen} onOpenChange={onOpenChange}>
      <Collapsible.Trigger
        className={cn(
          "text-muted-foreground/70 hover:text-foreground group flex w-full items-center gap-1 text-left text-caption-md-regular transition-colors",
          triggerClassName,
        )}
      >
        <DisclosureChevron className="size-3 text-inherit" />
        {label}
        {count != null && <span className="tabular-nums">· {count}</span>}
      </Collapsible.Trigger>
      <Collapsible.Panel className={cn("pt-3 pl-4", panelClassName)}>{children}</Collapsible.Panel>
    </Collapsible.Root>
  );
}
