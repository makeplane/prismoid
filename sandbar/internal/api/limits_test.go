// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package api

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// A JSON body past maxJSONBytes is refused before it is held in memory; one under it is served.
func TestAJSONBodyIsBounded(t *testing.T) {
	h := newServer()
	if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"j1","spec":{"image":"x"}}`)); rr.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rr.Code, rr.Body)
	}
	huge := `{"command":"` + strings.Repeat("x", maxJSONBytes) + `"}`
	if rr := do(t, h, "POST", "/v1/sandboxes/j1/exec", "secret", strings.NewReader(huge)); rr.Code != http.StatusBadRequest ||
		!strings.Contains(rr.Body.String(), "too large") {
		t.Fatalf("an oversized exec body must be refused: %d %.200s", rr.Code, rr.Body)
	}
	long := `{"command":"` + strings.Repeat("x", 1<<20) + `"}`
	if rr := do(t, h, "POST", "/v1/sandboxes/j1/exec", "secret", strings.NewReader(long)); rr.Code != http.StatusOK {
		t.Fatalf("a long command under the bound runs: %d %.200s", rr.Code, rr.Body)
	}
}

// At most maxLargeWrites large file bodies are held at once: the next waits for a slot, and a small
// write never waits.
func TestLargeWritesWaitForASlot(t *testing.T) {
	reg := core.NewRegistry("mem", &memBackend{files: map[string][]byte{}})
	s := New(reg, "secret", slog.New(slog.NewTextHandler(io.Discard, nil)))
	h := s.Handler()
	if rr := do(t, h, "POST", "/v1/sandboxes", "secret", strings.NewReader(`{"id":"w1","spec":{"image":"x"}}`)); rr.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rr.Code, rr.Body)
	}
	for range maxLargeWrites {
		s.largeWrites <- struct{}{} // every slot held, as by writes still reading their bodies
	}
	small := do(t, h, "PUT", "/v1/sandboxes/w1/files?path=/work/s", "secret", strings.NewReader("small"))
	if small.Code != http.StatusOK {
		t.Fatalf("a small write never waits: %d %s", small.Code, small.Body)
	}

	done := make(chan int, 1)
	go func() {
		req := httptest.NewRequest("PUT", "/v1/sandboxes/w1/files?path=/work/big", bytes.NewReader(make([]byte, largeWrite+1)))
		req.Header.Set("Authorization", "Bearer secret")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		done <- rec.Code
	}()
	select {
	case code := <-done:
		t.Fatalf("a large write must wait while every slot is held, got %d", code)
	case <-time.After(100 * time.Millisecond):
	}
	<-s.largeWrites // one write finishes
	if code := <-done; code != http.StatusOK {
		t.Fatalf("the waiting write runs once a slot frees: %d", code)
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest("PUT", "/v1/sandboxes/w1/files?path=/work/gone", bytes.NewReader(make([]byte, largeWrite+1))).WithContext(ctx)
	req.Header.Set("Authorization", "Bearer secret")
	for len(s.largeWrites) < maxLargeWrites {
		s.largeWrites <- struct{}{}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req) // returns: a caller that left gives up its place
}
