/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, unwrap } from "~/lib/api/client";
import {
  CHAT_EVENT_TYPES,
  type ChatLive,
  type ChatState,
  type MessageOut,
  foldLive,
  foldMessage,
  foldMessages,
  initialChat,
} from "~/lib/chat-stream";
import { conversationStreamUrl, isFinalRefusal } from "~/lib/stream-ticket";

const FALLBACK_POLL_MS = 3000; // only while the socket is down - the stream is the primary
// A dropped socket is reopened from the fallback tick with a fresh ticket, backing off to this
// ceiling.
const REOPEN_MAX_MS = 60_000;

export type ConversationStream = {
  state: ChatState;
  connected: boolean;
  /** The record has been read at least once (the socket opened, or a poll answered - an
   *  empty thread counts): the composer may show. */
  ready: boolean;
  /** Read the record past the cursor now - after a send, after a gate resolves, or as the
   *  disconnected fallback. Idempotent: the fold dedupes by seq. */
  refresh: () => Promise<void>;
  /** Fold a local change through the same state (a proposal's status after a tap). */
  foldRows: (fn: (rows: MessageOut[]) => MessageOut[]) => void;
};

/**
 * The conversation's two-lane socket, the task stream's hook one table over:
 * `message` frames are the durable rows (replayed from 0, then followed; a dropped socket is
 * reopened with a fresh single-use ticket - lib/stream-ticket.ts, the session token never rides
 * the URL - asking from the newest seq it holds). A mount starts from 0 on purpose, unlike the task stream: a message row can
 * change after it lands (a proposal card's status) without a new seq, so rows kept from an
 * earlier visit could be stale. `chat.*` frames are the reply as it is
 * written. Frames coalesce into one render per animation frame. The socket lives until the
 * conversation changes or the component unmounts - never closed on a reply's end, because a
 * conversation at rest is the one about to continue.
 */
export function useConversationStream(conversationId: string): ConversationStream {
  const [state, setState] = useState<ChatState>(initialChat);
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState(false);
  const stateRef = useRef(state);
  const rafScheduled = useRef(false);
  const connectedRef = useRef(false);

  const schedule = useCallback(() => {
    if (rafScheduled.current) return;
    rafScheduled.current = true;
    requestAnimationFrame(() => {
      rafScheduled.current = false;
      setState(stateRef.current);
    });
  }, []);

  const refresh = useCallback(async () => {
    const fresh = await unwrap(
      api.GET("/v1/conversations/{conversation_id}/messages", {
        params: {
          path: { conversation_id: conversationId },
          query: { since: stateRef.current.lastSeq },
        },
      }),
    );
    setReady(true);
    if (fresh.length) {
      stateRef.current = foldMessages(stateRef.current, fresh);
      schedule();
    }
  }, [conversationId, schedule]);

  const foldRows = useCallback(
    (fn: (rows: MessageOut[]) => MessageOut[]) => {
      stateRef.current = {
        ...stateRef.current,
        rows: fn(stateRef.current.rows),
      };
      schedule();
    },
    [schedule],
  );

  useEffect(() => {
    stateRef.current = initialChat();
    setState(stateRef.current);
    setReady(false);

    let closed = false;
    let source: EventSource | null = null;
    let reopenAt = 0;
    let reopenWait = FALLBACK_POLL_MS;

    const onFrame = (e: MessageEvent) => {
      let frame: unknown;
      try {
        frame = JSON.parse(e.data);
      } catch {
        return; // a malformed frame never tears the stream down
      }
      const f = frame as { type?: string; seq?: number };
      try {
        stateRef.current =
          typeof f.seq === "number" && !f.type?.startsWith("chat.")
            ? foldMessage(stateRef.current, frame as MessageOut)
            : foldLive(stateRef.current, frame as ChatLive);
      } catch (err) {
        console.warn("dropped unfoldable chat frame", err);
        return;
      }
      schedule();
    };

    const attach = (url: string) => {
      if (closed) return;
      const s = new EventSource(url);
      for (const type of CHAT_EVENT_TYPES) s.addEventListener(type, onFrame as EventListener);
      s.onopen = () => {
        connectedRef.current = true;
        reopenWait = FALLBACK_POLL_MS;
        setConnected(true);
        setReady(true);
      };
      s.onerror = () => {
        // A ticket opens one stream once, so the browser's own retry would carry a spent one:
        // close it, and let the fallback tick poll the record and reopen with a fresh ticket.
        connectedRef.current = false;
        s.close();
        if (closed) return;
        setConnected(false);
        reopenLater();
      };
      source = s;
    };
    const reopenLater = () => {
      reopenAt = Date.now() + reopenWait;
      reopenWait = Math.min(reopenWait * 2, REOPEN_MAX_MS);
    };
    const open = () => {
      // A failed mint backs off like a dropped socket; a dead session's 401 has already signed
      // the tab out (lib/api/client.ts).
      // A refusal that answers the same forever (the conversation is gone) ends the stream.
      conversationStreamUrl(conversationId, stateRef.current.lastSeq).then(
        attach,
        (err: unknown) => {
          if (!closed && !isFinalRefusal(err)) reopenLater();
        },
      );
    };
    open();

    // The disconnected fallback: while the socket is down, read the record on a slow cadence,
    // and reopen it once its back-off has passed.
    const fallback = window.setInterval(() => {
      if (connectedRef.current) return;
      void refresh().catch(() => undefined);
      if (reopenAt > 0 && Date.now() >= reopenAt) {
        reopenAt = 0;
        source?.close();
        open();
      }
    }, FALLBACK_POLL_MS);

    return () => {
      closed = true;
      source?.close();
      window.clearInterval(fallback);
    };
  }, [conversationId, refresh, schedule]);

  return { state, connected, ready, refresh, foldRows };
}
