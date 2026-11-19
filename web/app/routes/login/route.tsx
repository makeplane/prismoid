/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { data, Form, Link, redirect, useNavigation } from "react-router";
import { z } from "zod";
import { AuthCard } from "~/components/layout/auth-card";
import { FormError } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { api, ApiError, getToken, storeSession, unwrap } from "~/lib/api/client";
import { safePath, safeNext, signupHref, zodFieldErrors } from "~/lib/auth";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Sign in · Prismoid" }];
}

const schema = z.object({
  email: z.string().email("Enter a valid email address."),
  password: z.string().min(1, "Enter your password."),
});

export function clientLoader({ request }: Route.ClientLoaderArgs) {
  const next = new URL(request.url).searchParams.get("next");
  // Already signed in → skip the form. If the token turns out stale, the shell guard bounces back.
  if (getToken()) throw redirect(safeNext(next));
  // Only a path of ours reaches the form: a hostile value is never echoed into the page.
  return { next: safePath(next) ?? "" };
}

type ActionResult = {
  fieldErrors: Record<string, string>;
  formError: string | null;
};

export async function clientAction({ request }: Route.ClientActionArgs) {
  const form = await request.formData();
  const next = safeNext(form.get("next") as string | null);
  const parsed = schema.safeParse({
    email: form.get("email"),
    password: form.get("password"),
  });
  if (!parsed.success) {
    return data<ActionResult>(
      { fieldErrors: zodFieldErrors(parsed.error), formError: null },
      { status: 400 },
    );
  }
  try {
    const session = await unwrap(api.POST("/v1/auth/login", { body: parsed.data }));
    storeSession(session.token, session.expires_at);
    throw redirect(next);
  } catch (e) {
    if (e instanceof Response) throw e; // the redirect
    if (e instanceof ApiError) {
      if (e.status === 422 && e.fields) {
        return data<ActionResult>({ fieldErrors: e.fields, formError: null }, { status: 422 });
      }
      const formError =
        e.status === 401 || e.status === 400
          ? "Invalid email or password."
          : e.status === 429
            ? `Too many attempts. Try again in ${e.retryAfter ?? 30}s.`
            : e.message;
      return data<ActionResult>({ fieldErrors: {}, formError }, { status: e.status });
    }
    return data<ActionResult>(
      { fieldErrors: {}, formError: "Something went wrong. Please try again." },
      { status: 500 },
    );
  }
}

// Run the loader on initial hydration (SPA) so a direct load/refresh renders with data,
// not a transient null. Blocks on the root HydrateFallback until it resolves.
clientLoader.hydrate = true as const;

export default function Login({ loaderData, actionData }: Route.ComponentProps) {
  const nav = useNavigation();
  const busy = nav.state === "submitting";
  const fe = actionData?.fieldErrors ?? {};

  return (
    <AuthCard
      title="Welcome back"
      subtitle="Sign in to your Prismoid workspace."
      footer={
        <>
          New here?{" "}
          <Link
            to={signupHref(loaderData.next)}
            className="text-foreground font-medium underline underline-offset-4"
          >
            Create an account
          </Link>
        </>
      }
    >
      <Form method="post" className="space-y-4" replace>
        <input type="hidden" name="next" value={loaderData.next} />
        <FormError>{actionData?.formError}</FormError>
        <InputField
          size="lg"
          orientation="vertical"
          label="Email"
          error={fe.email}
          id="email"
          name="email"
          type="email"
          autoComplete="email"
          required
          autoFocus
          placeholder="you@example.com"
        />
        <InputField
          size="lg"
          orientation="vertical"
          label="Password"
          error={fe.password}
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
        />
        <div className="text-right text-body-sm-regular">
          <Link
            to="/forgot-password"
            className="text-muted-foreground hover:text-foreground underline underline-offset-4"
          >
            Forgot your password?
          </Link>
        </div>
        <Button
          type="submit"
          variant="primary"
          size="md"
          stretch="full"
          loading={busy}
          label="Sign in"
        />
      </Form>
    </AuthCard>
  );
}
