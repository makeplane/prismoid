/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { FormError } from "~/components/kit";
import { AuthCard } from "~/components/layout/auth-card";
import { api, ApiError, getToken, unwrap } from "~/lib/api/client";
import { loginHref } from "~/lib/auth";
import type { Route } from "./+types/route";
import { forgetCode, keepCode, readCode } from "./verify-code";

export function meta() {
  return [{ title: "Verify email · Prismoid" }];
}

export function clientLoader() {
  return { signedIn: Boolean(getToken()) };
}
clientLoader.hydrate = true as const;

type Phase = "working" | "done" | "expired" | "failed";

// The expired page's own failure: the code is already gone, so a reload would not help.
const RESEND_FAILED = "We couldn't send a new link. Try again in a few minutes.";

/** The page the emailed link opens. The code arrives in `location.hash`: signed out it waits in
 *  sessionStorage through sign-in, signed in it is posted exactly once. */
export default function VerifyEmail({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>("working");
  const [resent, setResent] = useState(false);
  const [resendFailed, setResendFailed] = useState(false);
  const started = useRef(false); // an effect may run twice in dev; the code is single-use

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const code = readCode(window.location.hash, sessionStorage);
    window.history.replaceState(null, "", window.location.pathname);
    if (!code) return setPhase("expired");
    if (!loaderData.signedIn) {
      keepCode(code, sessionStorage);
      void navigate(loginHref("/verify-email"), { replace: true });
      return;
    }
    // Kept until the server answers for the code: a dead session's 401 signs out through the global
    // handler and the stored code comes back after sign-in; a 5xx leaves it for a retry.
    keepCode(code, sessionStorage);
    unwrap(api.POST("/v1/auth/me/verify-email", { body: { code } }))
      .then(() => {
        forgetCode(sessionStorage);
        setPhase("done");
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) return; // a dead session, not a bad code
        if (e instanceof ApiError && e.status === 422) {
          forgetCode(sessionStorage); // the code itself is refused: nothing left to retry
          setPhase("expired");
          return;
        }
        setPhase("failed");
      });
  }, [loaderData.signedIn, navigate]);

  const resend = () => {
    setResendFailed(false);
    return unwrap(api.POST("/v1/auth/me/verification")).then(
      () => setResent(true),
      () => setResendFailed(true),
    );
  };

  if (phase === "done") {
    return (
      <AuthCard title="Email verified">
        <div className="space-y-4">
          <p className="text-body-sm-regular">Your email address is verified.</p>
          <Button
            nativeButton={false}
            render={<Link to="/" />}
            variant="primary"
            size="md"
            stretch="full"
            label="Continue"
          />
        </div>
      </AuthCard>
    );
  }
  if (phase === "expired") {
    return (
      <AuthCard title="Verify your email">
        <div className="space-y-4">
          <p className="text-body-sm-regular">
            That link has expired. Send a new one from the banner.
          </p>
          {resent ? (
            <p className="text-muted-foreground text-body-sm-regular">Sent. Check your email.</p>
          ) : (
            <Button variant="secondary" size="md" stretch="full" label="Resend" onClick={resend} />
          )}
          <FormError>{resendFailed ? RESEND_FAILED : null}</FormError>
        </div>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Verify your email">
      <p className="text-muted-foreground text-body-sm-regular">
        {phase === "failed"
          ? "Something went wrong. Your link is kept: reload this page to try again."
          : "Verifying your email…"}
      </p>
    </AuthCard>
  );
}
