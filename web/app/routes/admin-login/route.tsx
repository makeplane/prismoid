/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The operator console's OWN front door (admin app only - routes.admin.ts). Same account
// spine as the product, but this door is narrower: after the password verifies, the scope is
// checked and a non-admin account is signed straight back out with the session revoked - a
// valid product login is NOT a valid console login. No signup, no reset links: accounts are
// not created here, and recovery lives on the product (the capability law - this screen
// offers only what this application can honor).

import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { ShieldCheck } from "lucide-react";
import {
  data,
  Form,
  redirect,
  useActionData,
  useLoaderData,
  useNavigation,
  type ClientActionFunctionArgs,
  type ClientLoaderFunctionArgs,
} from "react-router";
import { z } from "zod";
import { AuthCard } from "~/components/layout/auth-card";
import { FormError } from "~/components/kit";
import { api, ApiError, clearSession, getToken, storeSession, unwrap } from "~/lib/api/client";
import { safePath, safeNext, zodFieldErrors } from "~/lib/auth";

export function meta() {
  return [{ title: "Sign in · Prismoid Admin" }];
}

const schema = z.object({
  email: z.string().email("Enter a valid email address."),
  password: z.string().min(1, "Enter your password."),
});

export function clientLoader({ request }: ClientLoaderFunctionArgs) {
  const next = new URL(request.url).searchParams.get("next");
  // Already signed in → the shell guard decides (and drops a non-admin session itself).
  if (getToken()) throw redirect(safeNext(next));
  return { next: safePath(next) ?? "" };
}
clientLoader.hydrate = true as const;

type ActionResult = {
  fieldErrors: Record<string, string>;
  formError: string | null;
};

export async function clientAction({ request }: ClientActionFunctionArgs) {
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
    // THE console's own check: the credential is real, but is it an operator's? A product
    // account without the scope is refused here, session revoked - not bounced around.
    const me = await unwrap(api.GET("/v1/auth/me"));
    if (!me.is_admin) {
      try {
        await unwrap(api.POST("/v1/auth/logout", {}));
      } catch {
        // best-effort - the local session is cleared regardless
      }
      clearSession();
      return data<ActionResult>(
        {
          fieldErrors: {},
          formError: "This console is for operators. Your account doesn't hold the admin scope.",
        },
        { status: 403 },
      );
    }
    throw redirect(next);
  } catch (e) {
    if (e instanceof Response) throw e; // the redirect
    if (e instanceof ApiError) {
      const formError =
        e.status === 401 || e.status === 400
          ? "Invalid email or password."
          : e.status === 403
            ? "This account is suspended."
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

export default function AdminLogin() {
  const loaderData = useLoaderData<typeof clientLoader>();
  const actionData = useActionData<typeof clientAction>();
  const nav = useNavigation();
  const busy = nav.state === "submitting";
  const fe = actionData?.fieldErrors ?? {};

  return (
    <AuthCard
      title="Operator console"
      subtitle="Sign in with an admin account to manage Prismoid."
      footer={
        <span className="text-muted-foreground inline-flex items-center gap-1.5">
          <ShieldCheck className="size-3.5" />
          Admin accounts only
        </span>
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
          name="password"
          type="password"
          autoComplete="current-password"
          required
        />
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
