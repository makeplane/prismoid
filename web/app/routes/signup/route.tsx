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
import { getAuthConfig, signupPaused, type AuthConfig } from "~/lib/auth-config";
import { safePath, loginHref, safeNext, zodFieldErrors } from "~/lib/auth";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Create account · Prismoid" }];
}

// The 12-character rule is enforced here client-side (instant feedback) and again by the server;
// a server 422 maps back onto the same fields.
const schema = z.object({
  name: z.string().min(1, "Enter your name."),
  email: z.string().email("Enter a valid email address."),
  password: z.string().min(12, "Use at least 12 characters."),
});

export async function clientLoader({ request }: Route.ClientLoaderArgs) {
  const next = new URL(request.url).searchParams.get("next");
  if (getToken()) throw redirect(safeNext(next));
  // An invited person arrives with ?invite=<code> (from /invite/:code); it rides the form.
  return {
    next: safePath(next) ?? "",
    invite: new URL(request.url).searchParams.get("invite") ?? "",
    config: await getAuthConfig(),
  };
}

type ActionResult = {
  fieldErrors: Record<string, string>;
  formError: string | null;
  /** The server refused with signup_disabled: the page switches to its paused state. */
  paused?: boolean;
};

/** What a refused signup shows: field errors, the paused page, or a message. Any other 403 is the
 *  server's deliberately vague refusal of the address (it never says whether an account exists),
 *  or, with an invite, of an address the invite was not sent to: the form says so in its own words. */
function refusal(e: ApiError, invited: boolean) {
  if (e.status === 422 && e.fields) {
    return data<ActionResult>({ fieldErrors: e.fields, formError: null }, { status: 422 });
  }
  if (e.status === 403 && e.code === "signup_disabled") {
    return data<ActionResult>({ fieldErrors: {}, formError: null, paused: true }, { status: 403 });
  }
  const formError =
    e.status === 403
      ? invited
        ? "That email can't use this invite. Use the address it was sent to, or sign in if that email already has an account."
        : "That email can't be used to sign up. If it already has an account, sign in instead."
      : e.status === 409
        ? "An account with that email already exists."
        : e.status === 429
          ? `Too many attempts. Try again in ${e.retryAfter ?? 30}s.`
          : e.message;
  return data<ActionResult>({ fieldErrors: {}, formError }, { status: e.status });
}

export async function clientAction({ request }: Route.ClientActionArgs) {
  const form = await request.formData();
  const next = safeNext(form.get("next") as string | null);
  const invite = ((form.get("invite") as string | null) ?? "").trim();
  const parsed = schema.safeParse({
    name: form.get("name"),
    email: form.get("email"),
    password: form.get("password"),
  });
  // The paused page's form is the invited person's way in: without the code it is no signup at all.
  const codeMissing = form.get("invite_required") === "1" && !invite;
  if (!parsed.success || codeMissing) {
    const fieldErrors = parsed.success ? {} : zodFieldErrors(parsed.error);
    if (codeMissing) fieldErrors.invite_code = "Enter the code from your invite email.";
    return data<ActionResult>(
      { fieldErrors, formError: null, paused: codeMissing },
      { status: 400 },
    );
  }
  try {
    const session = await unwrap(
      api.POST("/v1/auth/signup", {
        body: { ...parsed.data, ...(invite ? { invite_code: invite } : {}) },
      }),
    );
    storeSession(session.token, session.expires_at);
    throw redirect(next);
  } catch (e) {
    if (e instanceof Response) throw e;
    if (e instanceof ApiError) return refusal(e, Boolean(invite));
    return data<ActionResult>(
      { fieldErrors: {}, formError: "Something went wrong. Please try again." },
      { status: 500 },
    );
  }
}

/** The invite code: an emailed invite's code pasted here joins that workspace at signup (a
 *  `/invite/:code` link pre-fills it). Optional while signups are open (empty = just your own
 *  workspace); `required` on the paused page, where it is the only way in. */
export function InviteCodeField({
  defaultValue,
  error,
  required = false,
}: {
  defaultValue: string;
  error?: string;
  required?: boolean;
}) {
  return (
    <InputField
      size="lg"
      orientation="vertical"
      label={required ? "Invite code" : "Invite code (optional)"}
      error={error}
      hint={
        required
          ? "The code from your invite email."
          : "Invited to a workspace? Paste the code to join it as you sign up."
      }
      name="invite"
      autoComplete="off"
      required={required}
      defaultValue={defaultValue}
      placeholder="From your invite email"
    />
  );
}

// Run the loader on initial hydration (SPA) so a direct load/refresh renders with data,
// not a transient null. Blocks on the root HydrateFallback until it resolves.
clientLoader.hydrate = true as const;

/** "By signing up you agree to the Terms and Privacy Policy." - only the links that are set. */
function ConsentLine({ config }: { config: AuthConfig }) {
  const links = [
    { label: "Terms", href: config.terms_url },
    { label: "Privacy Policy", href: config.privacy_url },
  ].filter((l): l is { label: string; href: string } => Boolean(l.href));
  if (links.length === 0) return null;
  return (
    <span className="mt-2 block">
      By signing up you agree to the{" "}
      {links.map((l, i) => (
        <span key={l.label}>
          {i > 0 && " and "}
          <a
            href={l.href}
            target="_blank"
            rel="noreferrer"
            className="text-foreground font-medium underline underline-offset-4"
          >
            {l.label}
          </a>
        </span>
      ))}
      .
    </span>
  );
}

type LoaderData = Route.ComponentProps["loaderData"];

/** The account form. `inviteRequired`: the paused page's form, where the emailed code is the way in. */
function SignupForm({
  loaderData,
  actionData,
  inviteRequired,
}: {
  loaderData: LoaderData;
  actionData: Route.ComponentProps["actionData"];
  inviteRequired: boolean;
}) {
  const nav = useNavigation();
  const busy = nav.state === "submitting";
  const fe = actionData?.fieldErrors ?? {};
  return (
    <Form method="post" className="space-y-4" replace>
      <input type="hidden" name="next" value={loaderData.next} />
      {inviteRequired && <input type="hidden" name="invite_required" value="1" />}
      <FormError>{actionData?.formError}</FormError>
      <InputField
        size="lg"
        orientation="vertical"
        label="Name"
        error={fe.name}
        name="name"
        autoComplete="name"
        required
        autoFocus
        placeholder="Ada"
      />
      <InputField
        size="lg"
        orientation="vertical"
        label="Email"
        error={fe.email}
        name="email"
        type="email"
        autoComplete="email"
        required
        placeholder="you@example.com"
      />
      <InputField
        size="lg"
        orientation="vertical"
        label="Password"
        error={fe.password}
        hint="At least 12 characters."
        name="password"
        type="password"
        autoComplete="new-password"
        required
      />
      <InviteCodeField
        defaultValue={loaderData.invite}
        error={fe.invite_code}
        required={inviteRequired}
      />
      <Button
        type="submit"
        variant="primary"
        size="md"
        stretch="full"
        loading={busy}
        label="Create account"
      />
    </Form>
  );
}

export default function Signup({ loaderData, actionData }: Route.ComponentProps) {
  const paused = Boolean(actionData?.paused) || signupPaused(loaderData.config, loaderData.invite);
  const footer = (
    <>
      Already have an account?{" "}
      <Link
        to={
          loaderData.invite
            ? loginHref(`/invite/${encodeURIComponent(loaderData.invite)}`)
            : loginHref(loaderData.next)
        }
        className="text-foreground font-medium underline underline-offset-4"
      >
        Sign in
      </Link>
      <ConsentLine config={loaderData.config} />
    </>
  );

  if (paused) {
    return (
      <AuthCard title="Signups are paused" footer={footer}>
        <div className="space-y-4">
          <p className="text-muted-foreground text-body-sm-regular">
            Prismoid isn't taking new accounts right now. If you have an invite, open the link in
            your email.
          </p>
          <p className="text-muted-foreground text-body-sm-regular">
            Got a code in your invite email? Paste it below to sign up.
          </p>
          <SignupForm loaderData={loaderData} actionData={actionData} inviteRequired />
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Create your account"
      subtitle="Start handing work to Prismoid."
      footer={footer}
    >
      <SignupForm loaderData={loaderData} actionData={actionData} inviteRequired={false} />
    </AuthCard>
  );
}
