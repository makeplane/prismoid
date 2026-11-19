/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { LoaderCircle } from "lucide-react";

import { cn } from "../internal/cn";

// Propel's CircularProgress needs value/size/variant and LogoSpinner is the brand mark, so the
// plain spinner stays a lucide glyph: it inherits currentColor, there is no token to map.
function Spinner({ className, ...props }: React.ComponentProps<"svg">) {
  return (
    <LoaderCircle
      data-slot="spinner"
      role="status"
      aria-label="Loading"
      className={cn("size-4 animate-spin", className)}
      {...props}
    />
  );
}

export { Spinner };
