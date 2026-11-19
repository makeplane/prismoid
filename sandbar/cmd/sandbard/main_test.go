// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package main

import (
	"context"
	"errors"
	"testing"

	"github.com/makeplane/prismoid/sandbar/internal/backend/docker"
	"github.com/makeplane/prismoid/sandbar/internal/core"
)

// The boot check finds the Docker backend through egressChecker. A method renamed on one side would
// otherwise leave sandbard booting on a bad frame network with every test green: this fails the
// build instead.
var _ egressChecker = (*docker.Backend)(nil)

type checkedBackend struct {
	core.Backend
	err   error
	calls int
}

func (c *checkedBackend) CheckEgress(context.Context) error { c.calls++; return c.err }

// With egress on, sandbard refuses to start without its relay or below the Engine's
// floor; with egress off, neither is consulted (no sandbox can ask for domains).
func TestTheBootCheckGuardsEgressOnlyWhenItIsOn(t *testing.T) {
	bad := &checkedBackend{err: errors.New("not internal")}
	if err := checkEgress(context.Background(), []core.Backend{bad}, true); err == nil {
		t.Fatal("egress on and a bad network must refuse to boot")
	}
	off := &checkedBackend{err: errors.New("not internal")}
	if err := checkEgress(context.Background(), []core.Backend{off}, false); err != nil || off.calls != 0 {
		t.Fatalf("egress off must not consult the network: %v %d", err, off.calls)
	}
	ok := &checkedBackend{}
	if err := checkEgress(context.Background(), []core.Backend{ok}, true); err != nil || ok.calls != 1 {
		t.Fatalf("a good network boots: %v %d", err, ok.calls)
	}
}

// An empty token turns auth off, so it boots only where nothing but this host can connect.
func TestAnOpenDaemonBootsOnlyOnLoopback(t *testing.T) {
	cases := []struct {
		addr, token string
		ok          bool
	}{
		{"127.0.0.1:7070", "", true},
		{"[::1]:7070", "", true},
		{"localhost:7070", "", true},
		{"0.0.0.0:7070", "", false},
		{":7070", "", false},
		{"10.0.0.5:7070", "", false},
		{"0.0.0.0:7070", "secret", true},
		{"not-an-address", "", false},
	}
	for _, c := range cases {
		if err := refuseOpenDaemon(c.addr, c.token); (err == nil) != c.ok {
			t.Errorf("refuseOpenDaemon(%q, %q) = %v, want ok=%v", c.addr, c.token, err, c.ok)
		}
	}
}
