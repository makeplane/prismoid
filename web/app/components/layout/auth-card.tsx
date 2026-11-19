/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type ReactNode } from "react";
import { BrandMark } from "~/components/kit";
import { ModeToggle } from "~/components/layout/mode-toggle";

/** The centered, branded frame the public auth screens (login, signup) share. */
export function AuthCard({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="relative flex min-h-svh items-center justify-center px-4">
      <div className="absolute right-4 top-4">
        <ModeToggle />
      </div>
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-2 text-center">
          <div className="flex justify-center">
            <BrandMark size="xl" />
          </div>
          <h1 className="font-heading text-h4-regular font-semibold tracking-tight">{title}</h1>
          {subtitle && <p className="text-muted-foreground text-body-sm-regular">{subtitle}</p>}
        </div>
        <div className="bg-card ring-foreground/10 rounded-xl p-6 shadow-raised-100 ring-1">
          {children}
        </div>
        {footer && (
          <p className="text-muted-foreground text-center text-body-sm-regular">{footer}</p>
        )}
      </div>
    </div>
  );
}
