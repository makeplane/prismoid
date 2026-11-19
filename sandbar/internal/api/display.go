// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package api

import (
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/ticket"
)

// displayProtocol is the Upgrade token of the display route: a plain HTTP upgrade to a raw byte
// pipe, not a WebSocket, so sandbar takes no dependency.
const displayProtocol = "sandbar-display"

// displayWriteTimeout bounds each write to the taken-over connection: Hijack clears net/http's
// deadlines, and a caller that stops reading must not pin the exec, the bridge and the Engine
// connection until TCP gives up. The Plane relay reads its upstream continuously.
const displayWriteTimeout = 30 * time.Second

// WithDisplay turns the display route on with v (nil leaves it off: 503). Returns s for chaining.
func (s *Server) WithDisplay(v *ticket.Verifier) *Server {
	s.tickets = v
	return s
}

// display is GET /v1/sandboxes/{id}/display: the screen's way into a sandbox.
// It answers 101 Switching Protocols and then pipes raw bytes between the caller and the
// browser sidecar's screen bridge (core.DisplayBridge), exec'd through the backend's Attacher.
// sandbar parses nothing on the pipe: the host-side input gate is the Plane API's relay. It rides
// an exec, never the sandbox's network, so a sandbox with no route out has a screen too.
//
// Only a display ticket admits a caller - never the master token, which this route does not
// read - and every other route refuses a ticket (Server.auth). The ticket is peeked first, then
// spent once the request has proved itself (the upgrade headers, the sandbox, a backend that can
// stream) and BEFORE the bridge is attached: one ticket starts at most one exec, however many
// requests race with it, and a replay never gets a 101. A failure after that still spends it -
// the Plane API mints a fresh ticket for every open. Neither the ticket nor its claims are logged.
//
// Attach runs before the hijack, so its failures are ordinary responses (writeCoreErr: a backend's
// ErrBadSpec is 400, an Engine failure 502). The request context lives until this handler returns
// (or the caller's connection fails) and the stream is bound to it, so a closed pipe always ends
// the exec. Hijack clears the connection's deadlines (sandbard sets only ReadHeaderTimeout), so a
// long-lived screen is not cut; each write gets its own (displayWriteTimeout).
func (s *Server) display(w http.ResponseWriter, r *http.Request) {
	if s.tickets == nil {
		writeErr(w, http.StatusServiceUnavailable, "display disabled: this daemon has no SANDBAR_DISPLAY_KEYS")
		return
	}
	raw, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Ticket ")
	if !ok {
		writeErr(w, http.StatusUnauthorized, "a display ticket is required")
		return
	}
	id := r.PathValue("id")
	claims, err := s.tickets.Peek(raw, id, time.Now())
	if err != nil {
		s.log.Warn("display ticket refused", "id", id, "reason", err.Error())
		writeErr(w, http.StatusUnauthorized, "display ticket refused: "+err.Error())
		return
	}
	if !hasToken(r.Header, "Connection", "upgrade") || !hasToken(r.Header, "Upgrade", displayProtocol) {
		writeErr(w, http.StatusBadRequest, "the display route needs Connection: Upgrade and Upgrade: "+displayProtocol)
		return
	}
	box, err := s.reg.Get(id)
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	att, ok := box.Instance().(core.Attacher)
	if !ok {
		writeErr(w, http.StatusNotImplemented, "this sandbox's backend cannot stream an exec")
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		writeErr(w, http.StatusInternalServerError, "this connection cannot be upgraded")
		return
	}
	if err := s.tickets.Redeem(claims, time.Now()); err != nil {
		s.log.Warn("display ticket refused", "id", id, "reason", err.Error())
		writeErr(w, http.StatusUnauthorized, "display ticket refused: "+err.Error())
		return
	}
	stream, err := att.Attach(r.Context(), core.DisplayBridge())
	if err != nil {
		writeCoreErr(w, err)
		return
	}
	defer stream.Close()
	conn, rw, err := hj.Hijack()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "this connection cannot be upgraded: "+err.Error())
		return
	}
	defer conn.Close()
	if _, err := rw.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: " +
		displayProtocol + "\r\n\r\n"); err != nil {
		return
	}
	if err := rw.Flush(); err != nil {
		return
	}
	start := time.Now()
	s.log.Info("display opened", "id", id)
	up, down := pipe(conn, rw.Reader, stream)
	s.log.Info("display closed", "id", id, "up_bytes", up, "down_bytes", down,
		"s", time.Since(start).Round(time.Second).Seconds())
}

// writeBound is a connection whose every Write must finish within d.
type writeBound struct {
	net.Conn
	d time.Duration
}

func (w writeBound) Write(p []byte) (int, error) {
	if err := w.Conn.SetWriteDeadline(time.Now().Add(w.d)); err != nil {
		return 0, err
	}
	return w.Conn.Write(p)
}

// pipe copies both ways until either side ends, then closes both, so neither copy outlives the
// other. client is the hijacked connection's reader: it may hold bytes the caller sent early.
func pipe(conn net.Conn, client io.Reader, stream io.ReadWriteCloser) (up, down int64) {
	done := make(chan struct{}, 2)
	go func() {
		up, _ = io.Copy(stream, client)
		done <- struct{}{}
	}()
	go func() {
		down, _ = io.Copy(writeBound{Conn: conn, d: displayWriteTimeout}, stream)
		done <- struct{}{}
	}()
	<-done
	_ = conn.Close()
	_ = stream.Close()
	<-done
	return up, down
}

// hasToken reports whether a comma-separated header carries token, case-insensitively
// (RFC 9110 §7.6.1 Connection, §7.8 Upgrade).
func hasToken(h http.Header, name, token string) bool {
	for _, v := range h.Values(name) {
		for _, t := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(t), token) {
				return true
			}
		}
	}
	return false
}
