/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { data, Form, Link, useNavigation } from "react-router";
import { z } from "zod";
import { AuthCard } from "~/components/layout/auth-card";
import { FormError } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { api, ApiError, unwrap } from "~/lib/api/client";
import { zodFieldErrors } from "~/lib/auth";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Forgot password · Prismoid" }];
}

const schema = z.object({
  email: z.string().email("Enter a valid email address."),
});

type ActionResult = {
  sent: boolean;
  email: string;
  fieldErrors: Record<string, string>;
  formError: string | null;
};

export async function clientAction({ request }: Route.ClientActionArgs) {
  const form = await request.formData();
  const parsed = schema.safeParse({ email: form.get("email") });
  if (!parsed.success) {
    return data<ActionResult>(
      {
        sent: false,
        email: "",
        fieldErrors: zodFieldErrors(parsed.error),
        formError: null,
      },
      { status: 400 },
    );
  }
  try {
    await unwrap(api.POST("/v1/auth/forgot-password", { body: parsed.data }));
    return data<ActionResult>({
      sent: true,
      email: parsed.data.email,
      fieldErrors: {},
      formError: null,
    });
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.status === 422 && e.fields) {
        return data<ActionResult>(
          { sent: false, email: "", fieldErrors: e.fields, formError: null },
          { status: 422 },
        );
      }
      const formError =
        e.status === 429 ? `Too many attempts. Try again in ${e.retryAfter ?? 30}s.` : e.message;
      return data<ActionResult>(
        { sent: false, email: "", fieldErrors: {}, formError },
        { status: e.status },
      );
    }
    return data<ActionResult>(
      {
        sent: false,
        email: "",
        fieldErrors: {},
        formError: "Something went wrong. Please try again.",
      },
      { status: 500 },
    );
  }
}

export default function ForgotPassword({ actionData }: Route.ComponentProps) {
  const nav = useNavigation();
  const busy = nav.state === "submitting";
  const fe = actionData?.fieldErrors ?? {};

  if (actionData?.sent) {
    return (
      <AuthCard
        title="Check your email"
        subtitle={`If ${actionData.email} has an account, a one-time reset code is on its way. It expires quickly and works once.`}
        footer={
          <>
            Wrong address?{" "}
            <Link
              to="/forgot-password"
              className="text-foreground font-medium underline underline-offset-4"
            >
              Try again
            </Link>
          </>
        }
      >
        <Button
          nativeButton={false}
          render={<Link to="/reset-password" />}
          variant="primary"
          size="md"
          stretch="full"
          label="I have my code"
        />
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Forgot your password?"
      subtitle="We'll email you a one-time code to set a new one."
      footer={
        <>
          Remembered it?{" "}
          <Link to="/login" className="text-foreground font-medium underline underline-offset-4">
            Sign in
          </Link>
        </>
      }
    >
      <Form method="post" className="space-y-4" replace>
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
        <Button
          type="submit"
          variant="primary"
          size="md"
          stretch="full"
          loading={busy}
          label="Send reset code"
        />
        <p className="text-muted-foreground text-center text-body-sm-regular">
          Already have a code?{" "}
          <Link
            to="/reset-password"
            className="text-foreground font-medium underline underline-offset-4"
          >
            Reset your password
          </Link>
        </p>
      </Form>
    </AuthCard>
  );
}
