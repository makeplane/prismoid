/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ThemeProvider } from "next-themes";
import { useEffect } from "react";
import {
  isRouteErrorResponse,
  Links,
  Meta,
  Outlet,
  Scripts,
  ScrollRestoration,
} from "react-router";
import { PrismoidToastProvider, toast } from "@prismoid/blocks/toast";
import { PageLoading } from "~/components/kit";
import { DevFooter } from "~/components/layout/dev-footer";
import { clearSession, setRateLimitedHandler, setUnauthorizedHandler } from "~/lib/api/client";
import { loginHref } from "~/lib/auth";
import type { Route } from "./+types/root";
import "./app.css";

export function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Prismoid</title>
        <Meta />
        <Links />
      </head>
      <body className="min-h-svh bg-background text-foreground antialiased">
        {/* Propel themes on data-theme (light | dark). */}
        <ThemeProvider
          attribute="data-theme"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          {/* Propel toasts (@prismoid/blocks/toast): one manager, the viewport rides this provider. */}
          <PrismoidToastProvider>{children}</PrismoidToastProvider>
        </ThemeProvider>
        <ScrollRestoration />
        <Scripts />
      </body>
    </html>
  );
}

/** SPA mode renders this into index.html at build time; shown until the client hydrates. */
export function HydrateFallback() {
  return (
    <div className="flex min-h-svh">
      <PageLoading label="Loading Prismoid…" />
    </div>
  );
}

export default function App() {
  // Wire the client's cross-cutting responses once, on the client. Any 401 (expired/revoked token)
  // clears the session and bounces to /login preserving the deep link; any 429 surfaces the live
  // Retry-After as a toast.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      clearSession();
      const here = window.location.pathname + window.location.search;
      const target = loginHref(here);
      if (!window.location.pathname.startsWith("/login")) window.location.href = target;
    });
    setRateLimitedHandler((retryAfter) => {
      toast.error("Rate limited", {
        description: `Too many requests. Try again in ${retryAfter ?? 30}s.`,
      });
    });
  }, []);

  return (
    <>
      <Outlet />
      <DevFooter />
    </>
  );
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  let title = "Something went wrong";
  let detail = "An unexpected error occurred.";
  if (isRouteErrorResponse(error)) {
    title = error.status === 404 ? "Not found" : `Error ${error.status}`;
    detail = error.status === 404 ? "That page does not exist." : error.statusText || detail;
  } else if (error instanceof Error) {
    detail = error.message;
  }
  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-3 p-6 text-center">
      <h1 className="font-heading text-h4-regular font-semibold tracking-tight">{title}</h1>
      <p className="text-muted-foreground max-w-md text-body-sm-regular">{detail}</p>
      <a href="/" className="text-body-sm-regular underline underline-offset-4">
        Back to safety
      </a>
    </main>
  );
}
