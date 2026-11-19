// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package docker

import (
	"context"
	"encoding/base64"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/egress"
)

// oneInstance is a backend that hands out one prepared instance, so a test drives a real
// instance and a real proxy through the registry's own egress versioning, with no Engine.
type oneInstance struct{ inst core.Instance }

func (o oneInstance) Name() string { return "docker" }
func (o oneInstance) Create(context.Context, string, core.Spec) (core.Instance, error) {
	return o.inst, nil
}

// refuse sends one CONNECT for host through the proxy with the sandbox's credential: the proxy
// refuses a host off the list and records the refusal.
func refuse(t *testing.T, p *egress.Proxy, token, host string) {
	t.Helper()
	req := httptest.NewRequest(http.MethodConnect, "http://"+host+":443", nil)
	req.Host = host + ":443"
	req.Header.Set("Proxy-Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte("x:"+token)))
	rr := httptest.NewRecorder()
	p.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("CONNECT %s: want 403, got %d", host, rr.Code)
	}
}

// A stale push (the 409) leaves the allowlist AND its refusals alone: only an accepted push starts a
// new audit. The registry refuses the stale version before the instance is asked, so the proxy's
// list and its denials are the ones the newer push left.
func TestAStaleEgressPushKeepsTheDenials(t *testing.T) {
	ctx := context.Background()
	proxy := egress.New(slog.New(slog.NewTextHandler(io.Discard, nil)), 5*time.Second)
	inst := &instance{b: &Backend{eg: proxy}, token: "tok-1"}
	reg := core.NewRegistry("docker", oneInstance{inst: inst})
	box, err := reg.Create(ctx, "s1", core.Spec{Image: "x", AllowDomains: []string{"a.example"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := box.SetEgress(ctx, []string{"a.example"}, 3); err != nil {
		t.Fatalf("version 3 applies: %v", err)
	}
	refuse(t, proxy, "tok-1", "b.example")

	if _, err := box.SetEgress(ctx, []string{"a.example", "b.example"}, 2); !errors.Is(err, core.ErrStale) {
		t.Fatalf("version 2 after 3 must be stale, got %v", err)
	}
	if d, _ := inst.Denials(ctx); len(d) != 1 || d[0].Host != "b.example" {
		t.Fatalf("a stale push keeps the denials, got %+v", d)
	}
	refuse(t, proxy, "tok-1", "b.example") // and the list it did not replace still refuses b.example

	if _, err := box.SetEgress(ctx, []string{"a.example", "b.example"}, 4); err != nil {
		t.Fatalf("version 4 applies: %v", err)
	}
	if d, _ := inst.Denials(ctx); len(d) != 0 {
		t.Fatalf("an accepted push clears the denials, got %+v", d)
	}
}
