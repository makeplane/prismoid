/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { AlertTriangle } from "lucide-react";
import { isRouteErrorResponse, Link, useRouteError } from "react-router";
import { Button } from "@makeplane/propel/components/button";
import { ApiError } from "~/lib/api/client";

/**
 * A route-level error boundary for the detail screens. Because it lives below the shell, a bad id or a
 * failed fetch shows a friendly message in the content area with the sidebar intact - not a whole-app
 * crash. Export it as `ErrorBoundary` from a route to opt in.
 */
export function RouteErrorBoundary() {
  const error = useRouteError();
  let title = "Couldn't load this";
  let detail = "Something went wrong.";
  if (isRouteErrorResponse(error)) {
    title = error.status === 404 ? "Not found" : `Error ${error.status}`;
    detail = error.statusText || detail;
  } else if (error instanceof ApiError) {
    title = error.status === 404 ? "Not found" : "Couldn't load this";
    detail = error.message;
  } else if (error instanceof Error) {
    detail = error.message;
  }
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <div className="bg-muted text-muted-foreground rounded-full p-2.5">
        <AlertTriangle className="size-5" />
      </div>
      <div className="space-y-1">
        <h1 className="font-heading text-h4-regular font-semibold tracking-tight">{title}</h1>
        <p className="text-muted-foreground max-w-sm text-body-sm-regular">{detail}</p>
      </div>
      <Button
        nativeButton={false}
        render={<Link to="/" />}
        variant="secondary"
        size="sm"
        stretch="auto"
        label="Back to dashboard"
      />
    </div>
  );
}
