/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { data, Form, Link, useNavigation } from "react-router";
import { z } from "zod";
import { AuthCard } from "~/components/layout/auth-card";
import { FormError } from "~/components/kit";
import { api, ApiError, unwrap } from "~/lib/api/client";
import { zodFieldErrors } from "~/lib/auth";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Reset password · Prismoid" }];
}

const schema = z.object({
  code: z.string().min(20, "Paste the whole code from the email (cr_…)."),
  password: z.string().min(12, "Use at least 12 characters."),
});

// A reset link may carry ?code=cr_… - prefill it so the user only types the new password.
export function clientLoader({ request }: Route.ClientLoaderArgs) {
  return { code: new URL(request.url).searchParams.get("code") ?? "" };
}
clientLoader.hydrate = true as const;

type ActionResult = {
  done: boolean;
  fieldErrors: Record<string, string>;
  formError: string | null;
};

export async function clientAction({ request }: Route.ClientActionArgs) {
  const form = await request.formData();
  const parsed = schema.safeParse({
    code: (form.get("code") as string | null)?.trim(),
    password: form.get("password"),
  });
  if (!parsed.success) {
    return data<ActionResult>(
      { done: false, fieldErrors: zodFieldErrors(parsed.error), formError: null },
      { status: 400 },
    );
  }
  try {
    // localAuthError: a 401 here means "bad/expired code" - the form's own error, rendered
    // below. The app-global handler would clear the session and bounce to /login mid-submit.
    await unwrap(
      api.POST("/v1/auth/reset-password", {
        body: { code: parsed.data.code, new_password: parsed.data.password },
      }),
      { localAuthError: true },
    );
    return data<ActionResult>({ done: true, fieldErrors: {}, formError: null });
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.status === 422 && e.fields) {
        // The API speaks new_password; the form field is password.
        const { new_password, ...rest } = e.fields;
        return data<ActionResult>(
          {
            done: false,
            fieldErrors: { ...rest, ...(new_password ? { password: new_password } : {}) },
            formError: null,
          },
          { status: 422 },
        );
      }
      const formError =
        e.status === 401
          ? "That code is invalid or has expired. Request a new one."
          : e.status === 429
            ? `Too many attempts. Try again in ${e.retryAfter ?? 30}s.`
            : e.message;
      return data<ActionResult>({ done: false, fieldErrors: {}, formError }, { status: e.status });
    }
    return data<ActionResult>(
      {
        done: false,
        fieldErrors: {},
        formError: "Something went wrong. Please try again.",
      },
      { status: 500 },
    );
  }
}

export default function ResetPassword({ loaderData, actionData }: Route.ComponentProps) {
  const nav = useNavigation();
  const busy = nav.state === "submitting";
  const fe = actionData?.fieldErrors ?? {};

  if (actionData?.done) {
    return (
      <AuthCard
        title="Password updated"
        subtitle="Every signed-in session was signed out and every API key was revoked. Sign in with your new password."
      >
        <Button
          nativeButton={false}
          render={<Link to="/login" />}
          variant="primary"
          size="md"
          stretch="full"
          label="Sign in"
        />
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Reset your password"
      subtitle="Paste the one-time code from the email, then choose a new password."
      footer={
        <>
          Need a code?{" "}
          <Link
            to="/forgot-password"
            className="text-foreground font-medium underline underline-offset-4"
          >
            Request one
          </Link>
        </>
      }
    >
      <Form method="post" className="space-y-4" replace>
        <FormError>{actionData?.formError}</FormError>
        <InputField
          size="lg"
          orientation="vertical"
          label="Reset code"
          error={fe.code}
          id="code"
          name="code"
          type="text"
          autoComplete="one-time-code"
          spellCheck={false}
          required
          autoFocus={!loaderData.code}
          defaultValue={loaderData.code}
          placeholder="cr_…"
        />
        <InputField
          size="lg"
          orientation="vertical"
          label="New password"
          error={fe.password}
          id="password"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          autoFocus={!!loaderData.code}
        />
        <Button
          type="submit"
          variant="primary"
          size="md"
          stretch="full"
          label="Reset password"
          loading={busy}
        />
      </Form>
    </AuthCard>
  );
}
