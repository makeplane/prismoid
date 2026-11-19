/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import * as React from "react";
import { mergeProps } from "@base-ui/react/merge-props";
import { useRender } from "@base-ui/react/use-render";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "../internal/cn";

function BubbleGroup({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="bubble-group"
      className={cn("flex min-w-0 flex-col gap-2", className)}
      {...props}
    />
  );
}

const bubbleVariants = cva(
  "group/bubble relative flex w-fit max-w-[80%] min-w-0 flex-col gap-1 group-data-[align=end]/message:self-end data-[align=end]:self-end data-[variant=ghost]:max-w-full",
  {
    variants: {
      variant: {
        default:
          "*:data-[slot=bubble-content]:bg-accent-primary *:data-[slot=bubble-content]:text-inverse [&>[data-slot=bubble-content]:is(button,a):hover]:bg-accent-primary-hover",
        secondary:
          "*:data-[slot=bubble-content]:bg-layer-1 *:data-[slot=bubble-content]:text-primary [&>[data-slot=bubble-content]:is(button,a):hover]:bg-layer-1-hover",
        muted:
          "*:data-[slot=bubble-content]:bg-layer-1 [&>[data-slot=bubble-content]:is(button,a):hover]:bg-layer-1-hover",
        tinted:
          "*:data-[slot=bubble-content]:bg-accent-subtle *:data-[slot=bubble-content]:text-primary [&>[data-slot=bubble-content]:is(button,a):hover]:bg-accent-subtle-hover",
        outline:
          "*:data-[slot=bubble-content]:border-subtle *:data-[slot=bubble-content]:bg-surface-1 [&>[data-slot=bubble-content]:is(button,a):hover]:bg-layer-1 [&>[data-slot=bubble-content]:is(button,a):hover]:text-primary",
        ghost:
          "border-none *:data-[slot=bubble-content]:rounded-none *:data-[slot=bubble-content]:bg-transparent *:data-[slot=bubble-content]:p-0 [&>[data-slot=bubble-content]:is(button,a):hover]:bg-layer-1 [&>[data-slot=bubble-content]:is(button,a):hover]:text-primary",
        destructive:
          "*:data-[slot=bubble-content]:bg-danger-subtle *:data-[slot=bubble-content]:text-danger-primary [&>[data-slot=bubble-content]:is(button,a):hover]:bg-danger-subtle-hover",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Bubble({
  variant = "default",
  align = "start",
  className,
  ...props
}: React.ComponentProps<"div"> &
  VariantProps<typeof bubbleVariants> & {
    align?: "start" | "end";
  }) {
  return (
    <div
      data-slot="bubble"
      data-variant={variant}
      data-align={align}
      className={cn(bubbleVariants({ variant }), className)}
      {...props}
    />
  );
}

function BubbleContent({ className, render, ...props }: useRender.ComponentProps<"div">) {
  return useRender({
    defaultTagName: "div",
    props: mergeProps<"div">(
      {
        className: cn(
          "w-fit max-w-full min-w-0 overflow-hidden rounded-lg border border-transparent px-2.5 py-1.5 text-body-2xs-regular wrap-break-word group-data-[align=end]/bubble:self-end [button]:text-left [button,a]:transition-colors [button,a]:outline-none [button,a]:focus-visible:border-accent-strong [button,a]:focus-visible:ring-2 [button,a]:focus-visible:ring-accent-strong/30",
          className,
        ),
      },
      props,
    ),
    render,
    state: {
      slot: "bubble-content",
    },
  });
}

const bubbleReactionsVariants = cva(
  // The ring cuts the chip out of the surface it overlaps (was ring-card); Propel's ring colors
  // carry no layer step, so it reads the layer variable directly.
  "absolute z-10 flex w-fit shrink-0 items-center justify-center gap-1 rounded-full bg-layer-1 px-1.5 py-0.5 text-caption-md-regular ring-2 ring-(--bg-layer-2) has-[button]:p-0",
  {
    variants: {
      side: {
        top: "top-0 -translate-y-3/4",
        bottom: "bottom-0 translate-y-3/4",
      },
      align: {
        start: "left-3",
        end: "right-3",
      },
    },
    defaultVariants: {
      side: "bottom",
      align: "end",
    },
  },
);

function BubbleReactions({
  side = "bottom",
  align = "end",
  className,
  ...props
}: React.ComponentProps<"div"> & {
  align?: "start" | "end";
  side?: "top" | "bottom";
}) {
  return (
    <div
      data-slot="bubble-reactions"
      data-align={align}
      data-side={side}
      className={cn(bubbleReactionsVariants({ side, align }), className)}
      {...props}
    />
  );
}

export { BubbleGroup, Bubble, BubbleContent, BubbleReactions };
