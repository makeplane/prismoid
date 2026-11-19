/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// /join/:code - a Project join link (ADR-062). Logged out: on to login carrying this very
// link, so signing in lands back here. Logged in: accept it in the loader and open the Project - the
// server has already moved the session into the Project's workspace, so there is nothing to switch here.
// A refusal (unknown, expired, revoked, used up: one 404 in the server's words) is a face, not a
// crash.

import { Link, redirect } from "react-router";
import { AuthCard } from "~/components/layout/auth-card";
import { Button } from "@makeplane/propel/components/button";
import { api, ApiError, getToken, unwrap } from "~/lib/api/client";
import { loginHref } from "~/lib/auth";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Join project · Prismoid" }];
}

export async function clientLoader({ params }: Route.ClientLoaderArgs) {
  const code = params.code as string;
  if (!getToken()) {
    throw redirect(loginHref(`/join/${encodeURIComponent(code)}`));
  }
  let projectId: string;
  try {
    projectId = (await unwrap(api.POST("/v1/projects/invites/accept", { body: { code } })))
      .project_id;
  } catch (e) {
    if (e instanceof ApiError) return { error: e.message };
    throw e;
  }
  throw redirect(`/projects/${projectId}`);
}
clientLoader.hydrate = true as const;

export default function Join({ loaderData }: Route.ComponentProps) {
  return (
    <AuthCard title="Couldn't join" subtitle={loaderData.error}>
      <Button
        nativeButton={false}
        variant="primary"
        size="md"
        stretch="full"
        label="Go home"
        render={<Link to="/" />}
      />
    </AuthCard>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
