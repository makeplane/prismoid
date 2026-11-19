/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Toasts on Propel (ADR-074), the way Plane's @plane/blocks/toast does it: one module
// manager, one provider at the root. `toast` keeps the slice of sonner's API the app calls
// (toast, .success, .error, .info with description and one action), so moving off sonner was an
// import-path change at every call site.
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import {
  createToastManager,
  ToastProvider,
  type ToastVariant,
} from "@makeplane/propel/components/toast";
import { X } from "lucide-react";
import type { MouseEventHandler, ReactNode } from "react";

export const toastManager = createToastManager();

export type ToastOptions = {
  description?: ReactNode;
  action?: { label: string; onClick?: MouseEventHandler<HTMLElement> };
  /** Milliseconds on screen (sonner's name; Base UI's `timeout`). */
  duration?: number;
};

function add(variant: ToastVariant, title: ReactNode, options: ToastOptions = {}) {
  return toastManager.add({
    title,
    description: options.description,
    timeout: options.duration,
    data: { variant, primaryAction: options.action },
  });
}

export const toast = Object.assign(
  (title: ReactNode, options?: ToastOptions) => add("neutral", title, options),
  {
    success: (title: ReactNode, options?: ToastOptions) => add("success", title, options),
    error: (title: ReactNode, options?: ToastOptions) => add("danger", title, options),
    info: (title: ReactNode, options?: ToastOptions) => add("info", title, options),
  },
);

export function PrismoidToastProvider({ children }: { children?: ReactNode }) {
  return (
    <ToastProvider
      toastManager={toastManager}
      close={<IconButton variant="ghost" size="xs" aria-label="Close" icon={<Icon icon={X} />} />}
    >
      {children}
    </ToastProvider>
  );
}
