// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Command sandbard is the sandbar daemon: an HTTP server that creates and drives
// sandboxes through pluggable isolation backends. Run it beside its caller on a
// private network; point the caller at http://127.0.0.1:PORT.
//
// Configuration is environment-only (twelve-factor), documented in README:
//
//	SANDBAR_ADDR       listen address              (default 127.0.0.1:7070)
//	SANDBAR_TOKEN      bearer token; empty = off   (default off - dev only, refused on a non-loopback address)
//	SANDBAR_BACKEND    default backend             (default docker)
//	SANDBAR_BACKENDS   comma list to enable        (default docker)
//	SANDBAR_DOCKER_SOCK docker unix socket         (default /var/run/docker.sock)
//	SANDBAR_DOCKER_NET  container network mode for a sandbox with NO domains (default none)
//	SANDBAR_DOCKER_EGRESS_POOL  the range each sandbox with domains takes its own /28 network from
//	                    (default 10.213.0.0/16)
//	SANDBAR_RELAY_CONTAINER  the relay container joined to each of those networks (default sandbar-egress)
//	SANDBAR_EGRESS_URL  the relay as a sandbox reaches it (default http://sandbar-egress:7080)
//	SANDBAR_EGRESS_CONNECT_PORTS  ports a CONNECT tunnel may reach, comma list or "*" (default 443,80)
//	SANDBAR_DOCKER_*    sandbox limits: PIDS_LIMIT, NOFILE, NPROC, READONLY_ROOTFS, TMPFS, STORAGE_SIZE,
//	                    RUNTIME (see README, Configuration)
//	SANDBAR_FC_*        firecracker settings        (see firecracker backend)
//	SANDBAR_DISPLAY_KEYS display tickets' public keys (default empty = no display route):
//	                    "kid:base64,kid:base64", each a raw 32-byte Ed25519 key
//
//	sandbard relay      a sandbox network's door to the egress proxy:
//	  SANDBAR_RELAY_TO        the proxy, host:port   (required)
//	  SANDBAR_RELAY_ADDR      listen address          (default 0.0.0.0:7080)
//	  SANDBAR_RELAY_MAX_CONNS concurrent pipes        (default 4096)
//	  SANDBAR_RELAY_MAX_PER_PEER pipes per source IP, i.e. per sandbox (default 64)
//
// A Docker sandbox outlives the daemon: sandbard adopts what it made at boot and keeps it at exit
// (a restart - every deploy's - never costs a warm Machine its work). Firecracker is not a
// shipping backend; its microVMs are still torn down at exit.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/api"
	"github.com/makeplane/prismoid/sandbar/internal/backend/docker"
	"github.com/makeplane/prismoid/sandbar/internal/backend/firecracker"
	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/egress"
	"github.com/makeplane/prismoid/sandbar/internal/relay"
	"github.com/makeplane/prismoid/sandbar/internal/ticket"
)

func main() {
	// A display ticket issued before this moment plus the skew is refused: the jti cache lives in
	// memory, so a restart must not reopen a replay window (callers re-mint).
	started := time.Now()
	log := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	if len(os.Args) > 1 && os.Args[1] == "relay" {
		os.Exit(runRelay(log))
	}

	addr := env("SANDBAR_ADDR", "127.0.0.1:7070")
	token := os.Getenv("SANDBAR_TOKEN")
	defBackend := env("SANDBAR_BACKEND", "docker")
	enabled := strings.Split(env("SANDBAR_BACKENDS", "docker"), ",")
	displayKeys, err := ticket.ParseKeys(os.Getenv("SANDBAR_DISPLAY_KEYS"))
	if err != nil {
		log.Error("display keys", "err", err) // a typo stops the boot; the message names no key bytes
		os.Exit(1)
	}

	if err := refuseOpenDaemon(addr, token); err != nil {
		log.Error("auth", "err", err)
		os.Exit(1)
	}

	rootCtx, rootCancel := context.WithCancel(context.Background())
	defer rootCancel()
	eg, err := startEgress(rootCtx, log)
	if err != nil {
		log.Error("egress proxy failed to start", "err", err)
		os.Exit(1)
	}

	backends, err := buildBackends(enabled, log, eg)
	if err != nil {
		log.Error("backend setup failed", "err", err)
		os.Exit(1)
	}
	// Adoption joins the relay to every network again (~50 ms each at 300, ~90 ms at 500 on Engine
	// 29.4), and the API serves only after it: a caller that listed an empty registry would take
	// every warm Machine for gone. Hence a boot budget in minutes (compose's start_period matches).
	bootCtx, cancelBoot := context.WithTimeout(rootCtx, 5*time.Minute)
	err = checkEgress(bootCtx, backends, eg != nil)
	if err != nil {
		cancelBoot()
		log.Error("egress is on but a sandbox could not be held to its relay", "err", err)
		os.Exit(1)
	}
	reg := core.NewRegistry(defBackend, backends...)
	adopted, err := reg.Adopt(bootCtx)
	cancelBoot()
	if err != nil {
		log.Error("adopting the sandboxes an earlier sandbard made failed", "err", err)
		os.Exit(1)
	}
	log.Info("adopted the sandboxes an earlier sandbard made", "count", adopted)
	var display *ticket.Verifier // nil: the display route answers 503
	if len(displayKeys) > 0 {
		display = ticket.NewVerifier(displayKeys, started)
	}
	srv := &http.Server{
		Addr:              addr,
		Handler:           api.New(reg, token, log).WithDisplay(display).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Info("sandbard listening", "addr", addr, "default", defBackend, "backends", reg.Backends(),
			"display_keys", len(displayKeys))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("serve failed", "err", err)
			os.Exit(1)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Info("shutting down; keeping the sandboxes the next boot adopts")

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	reg.Shutdown(ctx)
}

// refuseOpenDaemon refuses to boot a daemon with no token on an address other machines can reach:
// an empty token turns auth off, which is only safe where nothing but this host can connect.
func refuseOpenDaemon(addr, token string) error {
	if token != "" {
		return nil
	}
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("SANDBAR_ADDR %q: %w", addr, err)
	}
	if ip := net.ParseIP(host); host == "localhost" || (ip != nil && ip.IsLoopback()) {
		return nil
	}
	return fmt.Errorf("SANDBAR_TOKEN is empty but SANDBAR_ADDR %q is not a loopback address: set a token "+
		"(openssl rand -hex 32) or listen on 127.0.0.1", addr)
}

// startEgress runs the allowlist proxy when SANDBAR_EGRESS=on, else returns nil. Off, a sandbox has
// NO route out and a spec that asks for allow_domains is refused - deny-by-default, loudly. On, one
// proxy is shared by every backend; each sandbox gets a per-sandbox credential and reaches only its
// own domains.
func startEgress(ctx context.Context, log *slog.Logger) (*egress.Proxy, error) {
	if !strings.EqualFold(os.Getenv("SANDBAR_EGRESS"), "on") {
		return nil, nil
	}
	eg := egress.New(log, 15*time.Second)
	if err := eg.SetConnectPorts(os.Getenv("SANDBAR_EGRESS_CONNECT_PORTS")); err != nil {
		return nil, err
	}
	bound, _, err := eg.Serve(ctx, env("SANDBAR_EGRESS_ADDR", "0.0.0.0:7080"))
	if err != nil {
		return nil, err
	}
	log.Info("egress proxy listening", "addr", bound, "container_url", env("SANDBAR_EGRESS_URL", defaultEgressURL))
	return eg, nil
}

// runRelay is `sandbard relay`: the frame network's one door to the egress proxy.
// Same binary, same image, no Docker socket and no API.
func runRelay(log *slog.Logger) int {
	r, addr, err := relay.FromEnv(os.Getenv)
	if err != nil {
		log.Error("relay config", "err", err)
		return 1
	}
	r.Log = log
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Error("relay listen failed", "addr", addr, "err", err)
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.Info("relay listening", "addr", ln.Addr().String(), "to", r.To, "max_conns", r.MaxConns)
	if err := r.Serve(ctx, ln); err != nil {
		log.Error("relay stopped", "err", err)
		return 1
	}
	return 0
}

func buildBackends(names []string, log *slog.Logger, eg *egress.Proxy) ([]core.Backend, error) {
	var out []core.Backend
	for _, name := range names {
		switch strings.TrimSpace(name) {
		case "docker":
			pool, err := docker.ParsePool(env("SANDBAR_DOCKER_EGRESS_POOL", docker.DefaultEgressPool))
			if err != nil {
				return nil, err
			}
			limits, err := docker.LimitsFromEnv(os.Getenv)
			if err != nil {
				return nil, err
			}
			opts := []docker.Option{
				docker.WithLimits(limits),
				docker.WithNetwork(env("SANDBAR_DOCKER_NET", "none")),
				docker.WithEgressPool(pool),
				docker.WithRelay(env("SANDBAR_RELAY_CONTAINER", docker.DefaultRelay)),
			}
			if eg != nil {
				opts = append(opts, docker.WithEgress(eg, env("SANDBAR_EGRESS_URL", defaultEgressURL)))
			}
			b := docker.New(env("SANDBAR_DOCKER_SOCK", "/var/run/docker.sock"), opts...)
			if eg != nil && !b.IPv6() {
				log.Info("this kernel has no IPv6: sandboxes with domains are created without the IPv6 sysctls (there is none to disable)")
			}
			out = append(out, b)
		case "firecracker":
			b, err := firecracker.New(firecracker.ConfigFromEnv(), log)
			if err != nil {
				return nil, err
			}
			out = append(out, b)
		case "":
			// tolerate trailing commas
		default:
			log.Warn("unknown backend, skipping", "name", name)
		}
	}
	if len(out) == 0 {
		return nil, errors.New("no backends enabled")
	}
	return out, nil
}

const defaultEgressURL = "http://sandbar-egress:7080" // the relay on each sandbox's network

// egressChecker is a backend that can say whether a sandbox with domains would be held to its
// relay: the Docker backend (main_test.go asserts it still is one, so a rename cannot quietly
// switch the boot check off).
type egressChecker interface {
	CheckEgress(context.Context) error
}

// checkEgress refuses to boot an egress-on daemon with no relay, or on an Engine that gives an
// internal inhibit_ipv4 network a gateway. A backend without the check
// (firecracker) is not consulted.
func checkEgress(ctx context.Context, backends []core.Backend, egressOn bool) error {
	if !egressOn {
		return nil
	}
	for _, b := range backends {
		if c, ok := b.(egressChecker); ok {
			if err := c.CheckEgress(ctx); err != nil {
				return err
			}
		}
	}
	return nil
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
