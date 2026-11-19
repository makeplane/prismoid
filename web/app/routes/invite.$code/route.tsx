/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// /invite/:code - an emailed invite link (spec 9). Logged out: on to signup carrying the code
// (the signup body's invite_code joins them at creation). Logged in: accept it here and go home;
// a refusal (the invite is for another email, expired, used) is a face, not a crash.

import { Button } from "@makeplane/propel/components/button";
import { redirect, useLoaderData, Link } from "react-router";
import { AuthCard } from "~/components/layout/auth-card";
import { api, ApiError, getToken, unwrap } from "~/lib/api/client";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Join workspace · Prismoid" }];
}

export async function clientLoader({ params }: Route.ClientLoaderArgs) {
  const code = params.code as string;
  if (!getToken()) {
    throw redirect(`/signup?invite=${encodeURIComponent(code)}`);
  }
  try {
    await unwrap(api.POST("/v1/workspaces/invites/accept", { body: { code } }));
  } catch (e) {
    if (e instanceof ApiError) return { error: e.message };
    throw e;
  }
  throw redirect("/");
}
clientLoader.hydrate = true as const;

export default function Invite() {
  const { error } = useLoaderData<typeof clientLoader>();
  return (
    <AuthCard title="Couldn't join" subtitle={error}>
      <Button
        variant="primary"
        size="md"
        stretch="full"
        nativeButton={false}
        render={<Link to="/" />}
        label="Go home"
      />
    </AuthCard>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
