// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package docker is sandbar's reference backend: a container per sandbox, talking to
// Docker's Engine API directly over its unix socket. No docker SDK - stdlib net/http
// over a unix dialer - so sandbar stays dependency-free and the wire protocol stays
// visible. It is the backend you develop against on any machine (including macOS,
// where Firecracker cannot run), and the behavioural reference the firecracker backend
// must match.
//
// Isolation posture: non-root workload, CapDrop ALL, no-new-privileges. A sandbox with no domains
// has network=none (no route at all). A sandbox with domains gets an INTERNAL Docker network of its
// own - a /28 of the operator's pool, made with inhibit_ipv4, so the host holds no address on it,
// whose one other member is `sandbard relay`, its way out to the egress proxy
// (ADR-063): a process that ignores HTTP_PROXY reaches nothing, the host's services and every
// other sandbox included. Containers and networks carry their sandbox's id, so a restarted sandbard
// adopts them (Adopt) instead of forgetting them. This is NOT the isolation a microVM gives - a
// shared kernel is a shared kernel - and the docs say so.
package docker

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/makeplane/prismoid/sandbar/internal/core"
	"github.com/makeplane/prismoid/sandbar/internal/egress"
)

// Egress is the subset of *egress.Proxy the backend needs - an interface so the backend does not
// import the whole proxy and tests can supply a spy.
type Egress interface {
	Allow(principal string, domains []string)
	Revoke(principal string)
	Denials(principal string) []egress.Denial
}

// Backend implements core.Backend.
type Backend struct {
	http    *http.Client
	netMode string   // container network mode when a sandbox has NO egress ("none" = airtight)
	eg      Egress   // the allowlist proxy, or nil (then allow_domains is refused, not ignored)
	egURL   string   // proxy URL the container reaches, WITHOUT credentials (e.g. http://sandbar-egress:7080)
	relay   string   // the relay container joined to each sandbox's network
	pool    *subnets // where each sandbox with domains takes its network's /28
	ipv6    bool     // the kernel has IPv6, so a sandbox with domains gets it switched off (read once, in New)
	sock    string   // the Engine's unix socket: Attach dials its own connection to take it over
	limits  Limits   // what bounds each sandbox beyond its spec (limits.go)
}

// kernelHasIPv6 reports whether the kernel sandboxes run on has IPv6 to disable. sandbard shares
// the Engine's kernel (on the host, or in a container beside it), so a kernel booted with
// ipv6.disable=1 shows no ipv6 tree under sysNet (/proc/sys/net) here either - and runc refuses a
// sysctl it cannot find, which would fail every sandbox with domains. Where sandbard sees no Linux
// kernel at all (macOS dev against a VM's Engine: no /proc/sys/net), the belt stays: those engines
// have IPv6, and a wrong guess fails the create loudly instead of leaving IPv6 on.
func kernelHasIPv6(sysNet string) bool {
	if _, err := os.Stat(sysNet); err != nil {
		return true
	}
	_, err := os.Stat(path.Join(sysNet, "ipv6", "conf", "all", "disable_ipv6"))
	return err == nil
}

// IPv6 reports whether this backend disables IPv6 in sandboxes with domains: false only on a
// kernel without IPv6, where there is none to disable (sandbard says so once at boot).
func (b *Backend) IPv6() bool { return b.ipv6 }

// DefaultRelay is the relay container joined to each sandbox's network, unless the operator names
// another (SANDBAR_RELAY_CONTAINER).
const DefaultRelay = "sandbar-egress"

// DefaultEgressPool is the range each sandbox's /28 comes from, unless the operator names another
// (SANDBAR_DOCKER_EGRESS_POOL): 4096 sandboxes with domains, never Docker's own default pools.
const DefaultEgressPool = "10.213.0.0/16"

// inhibitIPv4 is the bridge option that keeps the host's IPv4 address off a network.
const inhibitIPv4 = "com.docker.network.bridge.inhibit_ipv4"

// The labels that let a restarted sandbard find what it made: a container and its network carry the
// sandbox's id, and the container its spec (never its env).
const (
	labelID   = "sandbar.id"
	labelSpec = "sandbar.spec"
)

// netPrefix names a sandbox's own network: sandbar-net-<id>.
const netPrefix = "sandbar-net-"

// Option configures the backend.
type Option func(*Backend)

// WithNetwork sets the no-egress container network mode. Default "none" (no route out at all).
func WithNetwork(mode string) Option {
	return func(b *Backend) { b.netMode = mode }
}

// WithRelay names the relay container joined to each sandbox's network.
func WithRelay(container string) Option {
	return func(b *Backend) { b.relay = container }
}

// WithEgressPool sets the range each sandbox's /28 comes from (ParsePool checks it).
func WithEgressPool(pool *net.IPNet) Option {
	return func(b *Backend) { b.pool = newSubnets(pool) }
}

// WithEgress enables egress: a sandbox whose spec lists allow_domains gets its own internal network
// with the relay on it, a per-sandbox proxy credential, and `proxyURL` (the relay by the name it
// joins under, e.g. http://sandbar-egress:7080). Without this, a spec that asks for egress is
// refused rather than silently run with no route out.
func WithEgress(eg Egress, proxyURL string) Option {
	return func(b *Backend) { b.eg = eg; b.egURL = proxyURL }
}

// ParsePool reads SANDBAR_DOCKER_EGRESS_POOL: an IPv4 range of at least one /28.
func ParsePool(raw string) (*net.IPNet, error) {
	_, pool, err := net.ParseCIDR(raw)
	if err != nil {
		return nil, fmt.Errorf("SANDBAR_DOCKER_EGRESS_POOL must be a CIDR (e.g. %s), got %q", DefaultEgressPool, raw)
	}
	if ones, bits := pool.Mask.Size(); bits != 32 || ones > blockBits {
		return nil, fmt.Errorf("SANDBAR_DOCKER_EGRESS_POOL must be an IPv4 range of /%d or wider, got %q", blockBits, raw)
	}
	return pool, nil
}

// New dials the Docker socket at sock (e.g. /var/run/docker.sock).
func New(sock string, opts ...Option) *Backend {
	pool, _ := ParsePool(DefaultEgressPool)
	b := &Backend{
		http: &http.Client{
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					return (&net.Dialer{}).DialContext(ctx, "unix", sock)
				},
			},
			Timeout: 0, // per-request contexts own timeouts; exec can run minutes
		},
		netMode: "none",
		sock:    sock,
		relay:   DefaultRelay,
		pool:    newSubnets(pool),
		ipv6:    kernelHasIPv6("/proc/sys/net"),
		limits:  DefaultLimits(),
	}
	for _, o := range opts {
		o(b)
	}
	return b
}

func (b *Backend) Name() string { return "docker" }

// CheckEgress refuses to boot an egress-on daemon that could not hold a sandbox with domains to
// its one way out: the relay container must exist, and this Engine must give an
// internal inhibit_ipv4 network no gateway (the floor) - proved on a throwaway
// network from the pool, removed after. Every sandbox's own network is checked again when it is
// made (checkNetwork), so an engine that changes under a running daemon is still refused.
func (b *Backend) CheckEgress(ctx context.Context) error {
	err := b.do(ctx, http.MethodGet, "/containers/"+url.PathEscape(b.relay)+"/json", "", nil, nil)
	var ee *engineError
	switch {
	case errors.As(err, &ee) && ee.status == http.StatusNotFound:
		return fmt.Errorf("%w: the relay container %q does not exist - start it first (deploy/compose.prod.yaml's "+
			"sandbar-egress; by hand, `sandbard relay`, sandbar README, Egress) or name it in SANDBAR_RELAY_CONTAINER",
			core.ErrBackend, b.relay)
	case err != nil:
		return fmt.Errorf("%w: inspect the relay container %q: %v", core.ErrBackend, b.relay, err)
	}
	name, subnet, err := b.makeNetwork(ctx, "probe-"+randToken()[:8])
	if err != nil {
		return err
	}
	return b.dropNetwork(context.WithoutCancel(ctx), name, subnet)
}

// makeNetwork creates the internal network a sandbox with domains lives on - `sandbar-net-<id>`, the
// first free /28 of the pool, inhibit_ipv4 so the host holds no address on it - and checks it before
// anything joins it. A block another network holds (the Engine's "Pool overlaps") stays held and the
// next is tried; a network of this name that outlived its sandbox (a sandbard that died mid-create:
// no live sandbox holds it, since the registry refuses a taken id) is removed once; a network that
// fails the check is removed and its block freed.
func (b *Backend) makeNetwork(ctx context.Context, id string) (string, string, error) {
	name := netPrefix + id
	cleared := false
	for {
		subnet := b.pool.take()
		if subnet == "" {
			return "", "", fmt.Errorf("%w: the egress pool %s has no free /%d left - widen SANDBAR_DOCKER_EGRESS_POOL",
				core.ErrBackend, b.pool.base, blockBits)
		}
		body, _ := json.Marshal(map[string]any{
			"Name": name, "Driver": "bridge", "Internal": true, "EnableIPv6": false,
			"Options": map[string]string{inhibitIPv4: "true"},
			"IPAM":    map[string]any{"Config": []map[string]string{{"Subnet": subnet}}},
			"Labels":  map[string]string{labelID: id},
		})
		err := b.do(ctx, http.MethodPost, "/networks/create", "application/json", bytes.NewReader(body), nil)
		var ee *engineError
		if errors.As(err, &ee) && ee.status == http.StatusForbidden && strings.Contains(ee.msg, "overlaps") {
			continue // another network holds the block: it stays held, and the next is tried
		}
		if errors.As(err, &ee) && ee.status == http.StatusConflict && !cleared {
			b.pool.free(subnet)
			if err := b.dropNetwork(ctx, name, ""); err != nil {
				return "", "", err
			}
			cleared = true
			continue
		}
		if err != nil {
			b.pool.free(subnet)
			return "", "", fmt.Errorf("%w: create the network %q: %v", core.ErrBackend, name, err)
		}
		if err := b.checkNetwork(ctx, name); err != nil {
			_ = b.dropNetwork(context.WithoutCancel(ctx), name, subnet)
			return "", "", err
		}
		return name, subnet, nil
	}
}

// checkNetwork refuses a sandbox's network that is gone, is not internal, or gives the host an
// address. Not internal, a process that ignores HTTP_PROXY goes straight
// out; with a gateway, the host holds that address and the sandbox reaches every host service
// listening on it. Every IPAM entry is read, since inhibit_ipv4 does not cover an IPv6 gateway; an
// engine that took the option and reserved an IPv4 gateway anyway is named as below the floor
// (the floor), since no setting of sandbard's can help there.
func (b *Backend) checkNetwork(ctx context.Context, name string) error {
	var nw struct {
		Internal bool              `json:"Internal"`
		Options  map[string]string `json:"Options"`
		IPAM     struct {
			Config []struct {
				Gateway string `json:"Gateway"`
			} `json:"Config"`
		} `json:"IPAM"`
	}
	err := b.do(ctx, http.MethodGet, "/networks/"+url.PathEscape(name), "", nil, &nw)
	var gateways []string
	v4 := false
	for _, c := range nw.IPAM.Config {
		if c.Gateway != "" {
			gateways = append(gateways, c.Gateway)
			if ip := net.ParseIP(c.Gateway); ip != nil && ip.To4() != nil {
				v4 = true
			}
		}
	}
	var ee *engineError
	switch {
	case errors.As(err, &ee) && ee.status == http.StatusNotFound:
		return fmt.Errorf("%w: the network %q is gone", core.ErrBackend, name)
	case err != nil:
		return fmt.Errorf("%w: inspect the network %q: %v", core.ErrBackend, name, err)
	case !nw.Internal:
		return fmt.Errorf("%w: the Docker Engine made %q without --internal - a sandbox on it would have a "+
			"direct route out", core.ErrBackend, name)
	case v4 && nw.Options[inhibitIPv4] == "true":
		return fmt.Errorf("%w: the network %q was made with inhibit_ipv4 yet has a gateway (%s): this Docker "+
			"Engine reserves a gateway despite inhibit_ipv4 - upgrade the engine (verified on 29.4)",
			core.ErrBackend, name, strings.Join(gateways, ", "))
	case len(gateways) > 0:
		return fmt.Errorf("%w: the network %q gives the host an address (gateway %s) - a sandbox on it reaches "+
			"every host service listening there (an IPv6 gateway: turn off the daemon's default IPv6 for new "+
			"networks)", core.ErrBackend, name, strings.Join(gateways, ", "))
	}
	return nil
}

// joinRelay puts the relay on a sandbox's network under the name the proxy URL uses. Already there
// is fine: a restart joins it again, and the relay may have been recreated since.
func (b *Backend) joinRelay(ctx context.Context, network string) error {
	alias := "sandbar-egress"
	if u, err := url.Parse(b.egURL); err == nil && u.Hostname() != "" {
		alias = u.Hostname()
	}
	body, _ := json.Marshal(map[string]any{"Container": b.relay, "EndpointConfig": map[string]any{"Aliases": []string{alias}}})
	err := b.do(ctx, http.MethodPost, "/networks/"+url.PathEscape(network)+"/connect", "application/json", bytes.NewReader(body), nil)
	var ee *engineError
	if err == nil || errors.As(err, &ee) && ee.status == http.StatusForbidden && strings.Contains(ee.msg, "already exists") {
		return nil
	}
	return fmt.Errorf("%w: the relay %q could not join %s: %v", core.ErrBackend, b.relay, network, err)
}

// dropNetwork removes a sandbox's network once its container is gone - the relay leaves first,
// since a network with an endpoint cannot be removed - and frees its block. Gone already is done.
func (b *Backend) dropNetwork(ctx context.Context, name, subnet string) error {
	body, _ := json.Marshal(map[string]any{"Container": b.relay, "Force": true})
	_ = b.do(ctx, http.MethodPost, "/networks/"+url.PathEscape(name)+"/disconnect", "application/json", bytes.NewReader(body), nil)
	err := b.do(ctx, http.MethodDelete, "/networks/"+url.PathEscape(name), "", nil, nil)
	var ee *engineError
	if err != nil && !(errors.As(err, &ee) && ee.status == http.StatusNotFound) {
		return fmt.Errorf("%w: remove the network %q: %v", core.ErrBackend, name, err)
	}
	b.pool.free(subnet)
	return nil
}

// Create pulls-if-needed, creates a stopped container, and starts it running a sleep
// so it stays alive for exec. The container is the sandbox; teardown removes it.
func (b *Backend) Create(ctx context.Context, id string, spec core.Spec) (core.Instance, error) {
	name := "sandbar-" + id

	// Egress posture (deny-by-default): no allow_domains → network=none, airtight. With
	// allow_domains, the container gets an INTERNAL network of its own (checked: no gateway, so no
	// host address either) with the relay on it, a per-sandbox proxy credential, and HTTP(S)_PROXY
	// pointing at the relay - the ONLY other thing on its network, which pipes to the allowlist
	// proxy. A process that ignores the env reaches nothing, no other sandbox included.
	netMode := b.netMode
	env := envList(spec.Env)
	var token, network, subnet string
	if len(spec.AllowDomains) > 0 {
		if b.eg == nil {
			return nil, fmt.Errorf("%w: spec asks for egress but this daemon has no egress proxy "+
				"(run with SANDBAR_EGRESS=on)", core.ErrBadSpec)
		}
		var err error
		if network, subnet, err = b.makeNetwork(ctx, id); err != nil {
			return nil, err
		}
		if err := b.joinRelay(ctx, network); err != nil {
			_ = b.dropNetwork(context.WithoutCancel(ctx), network, subnet)
			return nil, err
		}
		token = randToken()
		netMode = network
		u := withCreds(b.egURL, id, token)
		env = append(env,
			"HTTP_PROXY="+u, "HTTPS_PROXY="+u, "http_proxy="+u, "https_proxy="+u,
			"NO_PROXY=localhost,127.0.0.1", "no_proxy=localhost,127.0.0.1",
		)
		b.eg.Allow(egress.TokenKey(token), spec.AllowDomains)
	}
	undo := func() {
		if token != "" {
			b.eg.Revoke(egress.TokenKey(token))
		}
		if network != "" {
			_ = b.dropNetwork(context.WithoutCancel(ctx), network, subnet)
		}
	}

	host := b.limits.hostConfig(netMode, spec)
	if token != "" && b.ipv6 {
		// IPv6 off in the sandbox's own network namespace: inhibit_ipv4 keeps the host's IPv4 address off the sandbox's network, but a bridge's fe80::
		// link-local would still reach host services bound to ::. Recent engines already leave an
		// IPv4-only network's interfaces without IPv6; this makes it hold on any engine. A kernel
		// without IPv6 has nothing to disable, and runc would refuse the sysctls: none are set.
		host["Sysctls"] = map[string]string{
			"net.ipv6.conf.all.disable_ipv6":     "1",
			"net.ipv6.conf.default.disable_ipv6": "1",
		}
	}
	kept := spec
	kept.Env = nil // the spec label is read back by a restart; an env may hold what no label should
	specLabel, _ := json.Marshal(kept)
	cfg := map[string]any{
		"Image": spec.Image, "Cmd": []string{"sleep", "infinity"}, "Tty": false,
		"WorkingDir": "/work", "Env": env, "HostConfig": host,
		"Labels": map[string]string{labelID: id, labelSpec: string(specLabel)},
	}
	body, _ := json.Marshal(cfg)
	var created struct {
		ID string `json:"Id"`
	}
	if err := b.do(ctx, http.MethodPost, "/containers/create?name="+name, "application/json",
		bytes.NewReader(body), &created); err != nil {
		undo()
		return nil, fmt.Errorf("%w: create: %v", core.ErrBackend, err)
	}
	if err := b.do(ctx, http.MethodPost, "/containers/"+created.ID+"/start", "", nil, nil); err != nil {
		_ = b.do(context.WithoutCancel(ctx), http.MethodDelete, "/containers/"+created.ID+"?force=true", "", nil, nil)
		undo()
		return nil, fmt.Errorf("%w: start: %v", core.ErrBackend, err)
	}
	return &instance{b: b, cid: created.ID, token: token, net: network, subnet: subnet}, nil
}

// Adopt finds what an earlier sandbard made on this Engine - every container labelled with a
// sandbox id - and keeps it (a restart, every deploy's, must not cost a warm Machine its
// work). A sandbox with domains keeps its network (checked again; a network that fails is removed
// with its sandbox, never kept) and the relay joins it again; its credential, read back from the
// container's env, is registered with an EMPTY allowlist - its caller's next versioned push sets
// the list, so a restart never widens one. A network no container holds (a create that died
// between the two) is removed. The pool holds every adopted network's block.
func (b *Backend) Adopt(ctx context.Context) ([]core.Adopted, error) {
	filter := "?filters=" + url.QueryEscape(`{"label":["`+labelID+`"]}`)
	var boxes []struct {
		ID      string            `json:"Id"`
		Created int64             `json:"Created"`
		Labels  map[string]string `json:"Labels"`
	}
	if err := b.do(ctx, http.MethodGet, "/containers/json"+filter+"&all=true", "", nil, &boxes); err != nil {
		return nil, fmt.Errorf("%w: list containers: %v", core.ErrBackend, err)
	}
	var nets []struct {
		Name   string            `json:"Name"`
		Labels map[string]string `json:"Labels"`
		IPAM   struct {
			Config []struct {
				Subnet string `json:"Subnet"`
			} `json:"Config"`
		} `json:"IPAM"`
	}
	if err := b.do(ctx, http.MethodGet, "/networks"+filter, "", nil, &nets); err != nil {
		return nil, fmt.Errorf("%w: list networks: %v", core.ErrBackend, err)
	}
	type held struct{ name, subnet string }
	netOf := make(map[string]held, len(nets))
	for _, n := range nets {
		h := held{name: n.Name}
		if len(n.IPAM.Config) > 0 {
			h.subnet = n.IPAM.Config[0].Subnet
			b.pool.hold(h.subnet)
		}
		netOf[n.Labels[labelID]] = h
	}

	out := make([]core.Adopted, 0, len(boxes))
	for _, box := range boxes {
		id := box.Labels[labelID]
		var spec core.Spec
		if err := json.Unmarshal([]byte(box.Labels[labelSpec]), &spec); err != nil || id == "" {
			continue // not one this sandbard made whole: never adopt what it cannot describe
		}
		inst := &instance{b: b, cid: box.ID}
		if n, ok := netOf[id]; ok {
			delete(netOf, id)
			inst.net, inst.subnet = n.name, n.subnet
			if err := b.checkNetwork(ctx, n.name); err != nil {
				_ = inst.Destroy(context.WithoutCancel(ctx)) // fail closed: never kept on a network it cannot vouch for
				continue
			}
			if b.eg != nil { // egress off now: no relay to join, and the sandbox reaches nothing
				if err := b.adoptEgress(ctx, inst); err != nil {
					return nil, err
				}
			}
		}
		out = append(out, core.Adopted{ID: id, Spec: spec, CreatedAt: time.Unix(box.Created, 0).UTC(), Instance: inst})
	}
	for _, n := range netOf {
		_ = b.dropNetwork(ctx, n.name, n.subnet)
	}
	return out, nil
}

// adoptEgress joins the relay to an adopted sandbox's network again and registers its credential,
// read back from its container's env, with an EMPTY allowlist until its caller's next push.
func (b *Backend) adoptEgress(ctx context.Context, inst *instance) error {
	if err := b.joinRelay(ctx, inst.net); err != nil {
		return err
	}
	token, err := b.tokenOf(ctx, inst.cid)
	if err != nil || token == "" {
		return err
	}
	inst.token = token
	b.eg.Allow(egress.TokenKey(token), nil)
	return nil
}

// tokenOf reads a sandbox's proxy credential back from its container's env (the password of its
// HTTPS_PROXY), "" when it has none.
func (b *Backend) tokenOf(ctx context.Context, cid string) (string, error) {
	var insp struct {
		Config struct {
			Env []string `json:"Env"`
		} `json:"Config"`
	}
	if err := b.do(ctx, http.MethodGet, "/containers/"+cid+"/json", "", nil, &insp); err != nil {
		return "", fmt.Errorf("%w: inspect %s: %v", core.ErrBackend, cid, err)
	}
	for _, kv := range insp.Config.Env {
		if raw, ok := strings.CutPrefix(kv, "HTTPS_PROXY="); ok {
			if u, err := url.Parse(raw); err == nil {
				token, _ := u.User.Password()
				return token, nil
			}
		}
	}
	return "", nil
}

// instance is one container.
type instance struct {
	b      *Backend
	cid    string
	token  string // egress principal, "" when this sandbox has no egress
	net    string // its own network (sandbar-net-<id>), "" when it has no egress
	subnet string // that network's block of the pool

	mu     sync.Mutex
	closed bool // set before Destroy revokes the egress token, so a racing Egress cannot resurrect it
}

// Denials reports what the egress proxy refused for this sandbox (empty when it has no egress).
func (i *instance) Denials(_ context.Context) ([]core.Denial, error) {
	if i.token == "" || i.b.eg == nil {
		return nil, nil
	}
	out := make([]core.Denial, 0)
	for _, d := range i.b.eg.Denials(egress.TokenKey(i.token)) {
		out = append(out, core.Denial{Host: d.Host, Method: d.Method, Reason: d.Reason, At: d.At})
	}
	return out, nil
}

// State asks the Engine whether the container runs. A container the Engine no
// longer knows is missing; any other failure is unknown - never stopped.
func (i *instance) State(ctx context.Context) core.State {
	var insp struct {
		State struct {
			Running bool `json:"Running"`
		} `json:"State"`
	}
	err := i.b.do(ctx, http.MethodGet, "/containers/"+i.cid+"/json", "", nil, &insp)
	var ee *engineError
	switch {
	case err == nil && insp.State.Running:
		return core.StateRunning
	case err == nil:
		return core.StateStopped
	case errors.As(err, &ee) && ee.status == http.StatusNotFound:
		return core.StateMissing
	}
	return core.StateUnknown
}

// Start runs a stopped container again. `docker stop` keeps the writable layer -
// /work, /browser - and the container keeps its env (the egress credential, whose allowlist the
// proxy never revoked) and its network, so only its processes are new: `sleep infinity`, and the
// browser sidecar on its next command. The relay joins its network again first (a recreated relay
// has left it). A running container is left as it is (the Engine's 304).
func (i *instance) Start(ctx context.Context) error {
	if i.net != "" {
		if err := i.b.joinRelay(ctx, i.net); err != nil {
			return err
		}
	}
	err := i.b.do(ctx, http.MethodPost, "/containers/"+i.cid+"/start", "", nil, nil)
	var ee *engineError
	switch {
	case err == nil, errors.As(err, &ee) && ee.status == http.StatusNotModified:
		return nil
	case errors.As(err, &ee) && ee.status == http.StatusNotFound:
		return fmt.Errorf("%w: its container is gone", core.ErrNotFound)
	}
	return fmt.Errorf("%w: start: %v", core.ErrBackend, err)
}

// Egress REPLACES this sandbox's egress allowlist while it runs. An empty domains list is
// allowed and means "permit nothing" - deny-by-default made explicit, same as Create.
func (i *instance) Egress(_ context.Context, domains []string) error {
	i.mu.Lock()
	closed := i.closed
	i.mu.Unlock()
	if closed {
		return fmt.Errorf("%w: sandbox is being destroyed", core.ErrBadSpec)
	}
	if i.token == "" || i.b.eg == nil {
		return fmt.Errorf("%w: this sandbox has no egress (it was created without allow_domains)", core.ErrBadSpec)
	}
	i.b.eg.Allow(egress.TokenKey(i.token), domains)
	return nil
}

// Attach runs req as a streaming exec (core.Attacher, the display route): the same user, env and
// plain `sh -c` as Exec, with stdin attached. Write feeds the process's stdin; Read drains its
// stdout, demultiplexed; stderr is discarded. The stream ends when the process exits, when Close is
// called, or when ctx ends. It has no timeout: req.TimeoutS is Exec's.
func (i *instance) Attach(ctx context.Context, req core.ExecRequest) (io.ReadWriteCloser, error) {
	uid, err := req.UID()
	if err != nil {
		return nil, err
	}
	id, err := i.createExec(ctx, strconv.Itoa(uid), envList(req.Env), []string{"/bin/sh", "-c", req.Command}, true)
	if err != nil {
		return nil, err
	}
	return i.b.attachExec(ctx, id)
}

// removeScript takes each argument back, as root (core.Remover). Root holds no capability in a
// sandbox (CapDrop ALL), so it removes as the OWNER of what the system gave: it unlocks the
// parent, makes the path's own directories writable (find never follows a link), removes it, and
// puts the parent's mode back - /task and /task/inputs stay 0555 and root's. A path that is not
// there is already removed. Its arguments are paths, never shell text.
const removeScript = `for p do
	[ -e "$p" ] || [ -L "$p" ] || continue
	d=${p%/*}
	m=$(stat -c %a "$d") || exit 1
	chmod u+w "$d" || exit 1
	find "$p" -type d -exec chmod u+w {} + 2>/dev/null
	rm -rf -- "$p"
	rc=$?
	chmod "$m" "$d"
	[ "$rc" -eq 0 ] || exit "$rc"
done`

// Remove takes back what the system gave the sandbox (core.Remover): every
// path below core.SystemRoot, checked again here, removed by one fixed script run as root.
func (i *instance) Remove(ctx context.Context, paths []string) error {
	for _, p := range paths {
		if err := core.SystemPath(p); err != nil {
			return err
		}
	}
	if len(paths) == 0 {
		return nil
	}
	res, err := i.execAsRoot(ctx, append([]string{"/bin/sh", "-c", removeScript, "sh"}, paths...)...)
	if err != nil {
		return fmt.Errorf("%w: remove: %v", core.ErrBackend, err)
	}
	if res.ExitCode != 0 {
		return fmt.Errorf("%w: remove: %s", core.ErrBackend, strings.TrimSpace(res.Stderr))
	}
	return nil
}

func (i *instance) Archive(ctx context.Context, paths []string, w io.Writer) error {
	// Compose one tar from Docker's per-path archives so the wire shape matches the
	// firecracker backend exactly (basename-rooted entries). The trailer is written ONLY on
	// success: a failed archive must end cut short, never as a complete-looking tar a caller
	// would take for the whole directory (a checkpoint that silently lost /work).
	tw := tar.NewWriter(w)
	for _, p := range paths {
		rc, err := i.b.stream(ctx, http.MethodGet, archiveURL(i.cid, p))
		if err != nil {
			if i.absent(ctx, err) {
				continue // an absent path contributes nothing - the one not-found that is no failure
			}
			return err
		}
		err = copyTar(tw, rc)
		rc.Close()
		if err != nil {
			return err
		}
	}
	return tw.Close()
}

// archiveURL is the Engine's archive route for one path, the path query-escaped: raw, `c++.md`
// arrives as `c  .md`, `a#b.md` as `a`, `100%.md` as nothing - a file the agent just wrote that
// the Engine then reports missing.
func archiveURL(cid, p string) string {
	return "/containers/" + cid + "/archive?path=" + url.QueryEscape(p)
}

// absent reports whether err is the Engine's not-found for a PATH in a container that still
// exists. A vanished container 404s the same route, so the container is asked before a
// not-found is taken for an absent directory.
func (i *instance) absent(ctx context.Context, err error) bool {
	var ee *engineError
	if !errors.As(err, &ee) || ee.status != http.StatusNotFound {
		return false
	}
	return i.b.do(ctx, http.MethodGet, "/containers/"+i.cid+"/json", "", nil, nil) == nil
}

func (i *instance) Extract(ctx context.Context, r io.Reader) error {
	// Docker extracts a tar at a directory; our tars are rooted at /.
	return i.b.do(ctx, http.MethodPut, archiveURL(i.cid, "/"), "application/x-tar", r, nil)
}

func (i *instance) Destroy(ctx context.Context) error {
	i.mu.Lock()
	i.closed = true // before Revoke, so a racing Egress sees closed and cannot resurrect the token
	i.mu.Unlock()
	if i.token != "" && i.b.eg != nil {
		i.b.eg.Revoke(egress.TokenKey(i.token)) // forget the allowlist before the container is gone
	}
	err := i.b.do(ctx, http.MethodDelete, "/containers/"+i.cid+"?force=true&v=true", "", nil, nil)
	var ee *engineError
	if err != nil && !(errors.As(err, &ee) && ee.status == http.StatusNotFound) {
		return err // the record stays, so a retry finds the container (and its network) again
	}
	if i.net != "" {
		return i.b.dropNetwork(ctx, i.net, i.subnet) // its own network goes with it
	}
	return nil
}

// ── Engine API plumbing ─────────────────────────────────────────────────────────

func (b *Backend) do(ctx context.Context, method, urlPath, contentType string, body io.Reader, out any) error {
	rc, err := b.request(ctx, method, urlPath, contentType, body)
	if err != nil {
		return err
	}
	defer rc.Close()
	if out == nil {
		_, _ = io.Copy(io.Discard, rc)
		return nil
	}
	return json.NewDecoder(rc).Decode(out)
}

func (b *Backend) stream(ctx context.Context, method, urlPath string) (io.ReadCloser, error) {
	return b.request(ctx, method, urlPath, "", nil)
}

func (b *Backend) request(ctx context.Context, method, urlPath, contentType string, body io.Reader) (io.ReadCloser, error) {
	// The host in the URL is ignored (unix dialer), but net/http needs a valid one.
	// v1.44 is the floor modern Docker Engines accept; older ones negotiate down.
	req, err := http.NewRequestWithContext(ctx, method, "http://docker/v1.44"+urlPath, body)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := b.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		return nil, &engineError{status: resp.StatusCode, msg: strings.TrimSpace(string(msg))}
	}
	return resp.Body, nil
}

// engineError is a non-2xx answer from the Engine API. The status lets a caller tell a
// not-found from a failure without matching the message text.
type engineError struct {
	status int
	msg    string
}

func (e *engineError) Error() string { return fmt.Sprintf("docker %d: %s", e.status, e.msg) }

func copyTar(tw *tar.Writer, r io.Reader) error {
	tr := tar.NewReader(r)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if hdr.Typeflag == tar.TypeReg {
			if _, err := io.Copy(tw, tr); err != nil {
				return err
			}
		}
	}
}

func envList(env map[string]string) []string {
	out := make([]string, 0, len(env))
	for k, v := range env {
		out = append(out, k+"="+v)
	}
	return out
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

// randToken mints a per-sandbox egress credential - 128 bits, unguessable, so knowing another
// sandbox's token is the only way to borrow its allowlist and there is no way to know it.
func randToken() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// withCreds injects userinfo into the proxy URL so the client sends Proxy-Authorization. The
// username (sandbox id) is cosmetic - it makes the proxy log readable; the password (token) is
// the allowlist key.
func withCreds(rawURL, user, token string) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return rawURL
	}
	u.User = url.UserPassword(user, token)
	return u.String()
}

// blockBits is the size of each sandbox's network: a /28, 14 addresses for two endpoints (the
// sandbox and the relay), small enough that a /16 pool holds 4096.
const blockBits = 28

// subnets hands each sandbox with domains a /28 of the operator's range, so no two sandboxes ever
// share a network and Docker's own default address pools are never drawn on (a default pool holds
// a few dozen networks). Taken blocks live in memory; a restart re-reads them from the networks
// it adopts.
type subnets struct {
	mu    sync.Mutex
	base  *net.IPNet
	taken map[string]bool
}

func newSubnets(base *net.IPNet) *subnets {
	return &subnets{base: base, taken: map[string]bool{}}
}

// take reserves the first free block, or "" when the range is spent.
func (s *subnets) take() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	ones, _ := s.base.Mask.Size()
	start := binary.BigEndian.Uint32(s.base.IP.To4())
	for i := uint32(0); i < 1<<(blockBits-ones); i++ {
		ip := make(net.IP, 4)
		binary.BigEndian.PutUint32(ip, start+i<<(32-blockBits))
		block := fmt.Sprintf("%s/%d", ip, blockBits)
		if !s.taken[block] {
			s.taken[block] = true
			return block
		}
	}
	return ""
}

// hold marks a block taken: one an adopted sandbox's network holds.
func (s *subnets) hold(block string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.taken[block] = true
}

func (s *subnets) free(block string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.taken, block)
}
