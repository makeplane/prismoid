/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button as BaseButton } from "@base-ui/react/button";
import { Button, type ButtonSize } from "@makeplane/propel/elements/button";
import { IconButton } from "@makeplane/propel/elements/icon-button";

import { cn } from "./cn";

/** The kit Button's variant names (web's ui/button), so a composite keeps the words its call sites speak. */
export type KitVariant = "default" | "outline" | "secondary" | "ghost" | "destructive" | "link";

const VARIANT = {
  default: "primary",
  outline: "secondary",
  secondary: "tertiary",
  ghost: "ghost",
  destructive: "danger-outline",
  link: "ghost",
} as const;

export type KitButtonProps = BaseButton.Props & {
  variant: KitVariant;
  size: ButtonSize;
  /** Propel's square IconButton box instead of the label Button. */
  square: boolean;
};

/**
 * Base UI's Button grafted onto Propel's Button or IconButton element, for a composite that keeps
 * the kit Button's API. Propel's elements take no className, so the composite's own className
 * reaches the DOM through the render prop, merged with Propel's classes by the Propel-aware cn:
 * a caller's color or radius still wins, as it did on the kit Button.
 */
export function KitButton({ variant, size, square, className, ...props }: KitButtonProps) {
  const render = (p: React.HTMLAttributes<HTMLElement>) => (
    <BaseButton
      data-slot="button"
      {...p}
      {...props}
      className={cn(
        p.className,
        "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-(--node-size)",
        className,
      )}
    />
  );
  return square ? (
    <IconButton variant={VARIANT[variant]} size={size} render={render} />
  ) : (
    <Button variant={VARIANT[variant]} size={size} stretch="auto" render={render} />
  );
}
