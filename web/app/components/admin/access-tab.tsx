/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The user detail page's Access tab: an account's credential inventory, and the door to end the
// live ones. METADATA only - when, from what, alive or not; the token is not on the wire, in
// this component, or anywhere a screen could show it.
//
// Revokes are per row and surgical (the detail page's Overview keeps the "sign out everywhere"
// hammer). Each one is audited server-side and refreshes both this list and the header counts.

import { KeyRound, MonitorSmartphone } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { deviceLine, expiryLabel } from "~/components/admin/access-copy";
import { ConfirmAction } from "~/components/admin/confirm";
import { EmptyState, LoadingState, Pill, Rows, Section } from "~/components/kit";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";

type SessionRow = components["schemas"]["UserSessionOut"];
type KeyRow = components["schemas"]["UserApiKeyOut"];

export function AccessTab({
  userId,
  onRevoked,
}: {
  userId: string;
  /** The header's live-session/key counts ride the loader - refresh them after a revoke. */
  onRevoked: () => void;
}) {
  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [keys, setKeys] = useState<KeyRow[] | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = <T,>(promise: Promise<T[]>, set: (rows: T[]) => void, what: string) =>
      void promise.then(
        (rows) => alive && set(rows),
        (e) => {
          if (!alive) return;
          toastError(e, `Couldn't read the ${what}.`);
          set([]); // an honest empty beats a spinner that never stops
        },
      );

    load(
      unwrap(
        api.GET("/v1/admin/users/{id}/sessions", {
          params: { path: { id: userId } },
        }),
      ),
      setSessions,
      "sessions",
    );
    load(
      unwrap(
        api.GET("/v1/admin/users/{id}/keys", {
          params: { path: { id: userId } },
        }),
      ),
      setKeys,
      "API keys",
    );
    return () => {
      alive = false;
    };
  }, [userId, nonce]);

  const revoked = () => {
    setNonce((n) => n + 1);
    onRevoked();
  };

  return (
    <div className="space-y-6">
      <Section title="Sessions">
        {sessions === null ? (
          <LoadingState />
        ) : sessions.length === 0 ? (
          <EmptyState
            compact
            icon={MonitorSmartphone}
            title="No sessions"
            description="Their sign-ins show up here, newest first."
          />
        ) : (
          <Rows as="ul">
            {sessions.map((s) => (
              <li
                key={s.id}
                className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-3"
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-body-sm-regular font-medium">
                    {deviceLine(s.user_agent)}
                  </span>
                  <span className="text-muted-foreground block truncate text-caption-md-regular">
                    signed in {ago(s.created_at)} · {expiryLabel(s.expires_at)}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <Pill tone={s.live ? "success" : "muted"}>
                    {s.live ? "live" : s.revoked_at ? "revoked" : "expired"}
                  </Pill>
                  {s.live && (
                    <ConfirmAction
                      trigger="Revoke"
                      destructive
                      title="Revoke this session?"
                      description="Signs that device out. An open stream severs within seconds."
                      onConfirm={async () => {
                        await unwrap(
                          api.POST("/v1/admin/users/{id}/sessions/{session_id}/revoke", {
                            params: {
                              path: { id: userId, session_id: s.id },
                            },
                          }),
                        );
                        toast.success("Session revoked.");
                        revoked();
                      }}
                    />
                  )}
                </span>
              </li>
            ))}
          </Rows>
        )}
      </Section>

      <Section title="API keys">
        {keys === null ? (
          <LoadingState />
        ) : keys.length === 0 ? (
          <EmptyState
            compact
            icon={KeyRound}
            title="No API keys"
            description="Keys they mint show up here, live or revoked."
          />
        ) : (
          <Rows as="ul">
            {keys.map((k) => {
              const live = k.revoked_at === null;
              return (
                <li
                  key={k.id}
                  className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-3"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-body-sm-regular font-medium">
                      {k.name}
                    </span>
                    <span className="text-muted-foreground block truncate text-caption-md-regular">
                      created {ago(k.created_at)} ·{" "}
                      {k.last_used_at ? `last used ${ago(k.last_used_at)}` : "never used"}
                    </span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    <Pill tone={live ? "success" : "muted"}>{live ? "live" : "revoked"}</Pill>
                    {live && (
                      <ConfirmAction
                        trigger="Revoke"
                        destructive
                        title={`Revoke the key “${k.name}”?`}
                        description="Requests with it fail immediately. This can't be undone."
                        onConfirm={async () => {
                          await unwrap(
                            api.POST("/v1/admin/users/{id}/keys/{key_id}/revoke", {
                              params: { path: { id: userId, key_id: k.id } },
                            }),
                          );
                          toast.success("API key revoked.");
                          revoked();
                        }}
                      />
                    )}
                  </span>
                </li>
              );
            })}
          </Rows>
        )}
      </Section>
    </div>
  );
}
