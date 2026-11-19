// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

package ticket

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// pythonTicket and pythonPublicKey pin the format to the API's own minter
// (backend/prismoid/operations/tickets.py): a display ticket it minted for sandbox "sb-1" at
// now=1800000000 under kid "k1", signing with the 32-byte seed 00 01 02 ... 1f. Regenerate from
// backend/ with:
//
//	PRISMOID_API_SCREEN_SIGNING_KEY='k1:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=' \
//	  uv run python -c 'from prismoid.operations import tickets; print(tickets.mint_display("sb-1", now=1800000000)); print(tickets.public_keys())'
//
// The first line is pythonTicket (its jti is random, so every run prints a different, equally
// valid ticket); the second prints the public key below (either base64 alphabet).
const (
	pythonTicket    = "dt_eyJleHAiOjE4MDAwMDAwMzAsImlhdCI6MTgwMDAwMDAwMCwianRpIjoiMGRjNGQxNDNkMmVjMjc0YzQ2MWVhYzMxNDUwMjk2NTgiLCJraWQiOiJrMSIsInNhbmRib3giOiJzYi0xIn0.0Tbki0WACA8B5We6_M_D60tV5OMfVlHSNP_Tqdut83RlpnTWoXo0e7Obr8cvHmy6X7nxR78IPbmzm4wCuX62AA"
	pythonPublicKey = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
)

func pythonSeed() []byte {
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i)
	}
	return seed
}

func newKey(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv
}

// mintRaw signs claims bytes under a domain, the way the API's minter does.
func mintRaw(priv ed25519.PrivateKey, domain string, claims []byte) string {
	sig := ed25519.Sign(priv, append([]byte(domain), claims...))
	return Prefix + base64.RawURLEncoding.EncodeToString(claims) + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func mint(t *testing.T, priv ed25519.PrivateKey, c Claims) string {
	t.Helper()
	raw, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	return mintRaw(priv, Domain, raw)
}

func TestTheAPIsMinterVerifiesHere(t *testing.T) {
	priv := ed25519.NewKeyFromSeed(pythonSeed())
	keys, err := ParseKeys("k1:" + pythonPublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if !keys["k1"].Equal(priv.Public()) {
		t.Fatalf("the published public key is not the seed's: %x", keys["k1"])
	}
	v := NewVerifier(keys, time.Unix(1_800_000_000-60, 0))
	c, err := v.Peek(pythonTicket, "sb-1", time.Unix(1_800_000_001, 0))
	if err != nil {
		t.Fatalf("the API's ticket must verify: %v", err)
	}
	if c.Sandbox != "sb-1" || c.Kid != "k1" || c.Iat != 1_800_000_000 || c.Exp != 1_800_000_030 || c.Jti == "" {
		t.Fatalf("claims: %+v", c)
	}
	if _, err := v.Peek(pythonTicket, "sb-2", time.Unix(1_800_000_001, 0)); !errors.Is(err, ErrMismatch) {
		t.Fatalf("another sandbox: want mismatch, got %v", err)
	}
}

func TestPeek(t *testing.T) {
	pub, priv := newKey(t)
	_, otherPriv := newKey(t)
	now := time.Unix(1_800_000_000, 0)
	v := NewVerifier(map[string]ed25519.PublicKey{"k1": pub}, now.Add(-time.Hour))
	good := Claims{Sandbox: "sb-1", Iat: now.Unix(), Exp: now.Unix() + 30, Jti: "j-good", Kid: "k1"}
	with := func(edit func(*Claims)) string {
		c := good
		edit(&c)
		return mint(t, priv, c)
	}
	goodRaw, _ := json.Marshal(good)
	tampered := strings.Replace(string(goodRaw), `"sb-1"`, `"sb-2"`, 1)
	goodTicket := mint(t, priv, good)
	_, goodSig, _ := strings.Cut(strings.TrimPrefix(goodTicket, Prefix), ".")

	cases := []struct {
		name    string
		ticket  string
		sandbox string
		at      time.Time
		want    error
	}{
		{"a good ticket", goodTicket, "sb-1", now, nil},
		{"still good at exp + skew", goodTicket, "sb-1", now.Add(35 * time.Second), nil},
		{"expired past exp + skew", goodTicket, "sb-1", now.Add(36 * time.Second), ErrExpired},
		{"issued ahead of this clock", with(func(c *Claims) { c.Iat += 6; c.Exp += 6 }), "sb-1", now, ErrExpired},
		{"issued ahead within the skew", with(func(c *Claims) { c.Iat += 5; c.Exp += 5 }), "sb-1", now, nil},
		{"a lifetime over 30 s", with(func(c *Claims) { c.Exp = c.Iat + 31 }), "sb-1", now, ErrLifetime},
		{"exp not after iat", with(func(c *Claims) { c.Exp = c.Iat }), "sb-1", now, ErrLifetime},
		{"another sandbox's ticket", goodTicket, "sb-2", now, ErrMismatch},
		{"signed by another key", mint(t, otherPriv, good), "sb-1", now, ErrSignature},
		{"claims swapped under the signature", Prefix + base64.RawURLEncoding.EncodeToString([]byte(tampered)) + "." + goodSig, "sb-2", now, ErrSignature},
		{"signed under the screen ticket's domain", mintRaw(priv, "percy-screen-v1\n", goodRaw), "sb-1", now, ErrSignature},
		{"an unknown kid", with(func(c *Claims) { c.Kid = "k9" }), "sb-1", now, ErrUnknownKid},
		{"a screen ticket's prefix", "st_" + strings.TrimPrefix(goodTicket, Prefix), "sb-1", now, ErrMalformed},
		{"no prefix", strings.TrimPrefix(goodTicket, Prefix), "sb-1", now, ErrMalformed},
		{"padded base64", goodTicket + "==", "sb-1", now, ErrMalformed},
		{"three parts", goodTicket + ".x", "sb-1", now, ErrMalformed},
		{"an unknown claim", mintRaw(priv, Domain, []byte(`{"sandbox":"sb-1","iat":1800000000,"exp":1800000030,"jti":"j","kid":"k1","task":"t"}`)), "sb-1", now, ErrMalformed},
		{"a missing jti", mintRaw(priv, Domain, []byte(`{"sandbox":"sb-1","iat":1800000000,"exp":1800000030,"kid":"k1"}`)), "sb-1", now, ErrMalformed},
		{"a float iat", mintRaw(priv, Domain, []byte(`{"sandbox":"sb-1","iat":1.8e9,"exp":1800000030,"jti":"j","kid":"k1"}`)), "sb-1", now, ErrMalformed},
		{"trailing data", mintRaw(priv, Domain, append(goodRaw, []byte(` {}`)...)), "sb-1", now, ErrMalformed},
		{"empty", "", "sb-1", now, ErrMalformed},
		{"too long to parse", Prefix + strings.Repeat("a", 3000), "sb-1", now, ErrMalformed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := v.Peek(tc.ticket, tc.sandbox, tc.at)
			if tc.want == nil && err != nil {
				t.Fatalf("want admitted, got %v", err)
			}
			if tc.want != nil && !errors.Is(err, tc.want) {
				t.Fatalf("want %v, got %v", tc.want, err)
			}
		})
	}
}

// A restart empties the jti cache, so a ticket issued before the verifier started PLUS the skew
// is refused (the API's own rule): a minting clock up to Skew ahead stamps
// a ticket the previous process may have spent with an iat up to Skew past this start, so a
// restart cannot reopen its replay window. A client re-mints.
func TestATicketFromBeforeTheStartIsStale(t *testing.T) {
	pub, priv := newKey(t)
	started := time.Unix(1_800_000_000, 0)
	v := NewVerifier(map[string]ed25519.PublicKey{"k1": pub}, started)
	at := started.Add(6 * time.Second)
	for _, iat := range []int64{started.Unix() - 6, started.Unix(), started.Unix() + 4} {
		old := mint(t, priv, Claims{Sandbox: "sb-1", Iat: iat, Exp: iat + 30, Jti: "j-old", Kid: "k1"})
		if _, err := v.Peek(old, "sb-1", at); !errors.Is(err, ErrStale) {
			t.Fatalf("issued at start%+d: want stale, got %v", iat-started.Unix(), err)
		}
	}
	edge := mint(t, priv, Claims{Sandbox: "sb-1", Iat: started.Unix() + 5, Exp: started.Unix() + 35, Jti: "j-edge", Kid: "k1"})
	if _, err := v.Peek(edge, "sb-1", at); err != nil {
		t.Fatalf("issued at the start plus the skew: want admitted, got %v", err)
	}
}

func TestPeekDoesNotConsumeAndRedeemDoes(t *testing.T) {
	pub, priv := newKey(t)
	now := time.Unix(1_800_000_000, 0)
	v := NewVerifier(map[string]ed25519.PublicKey{"k1": pub}, now.Add(-time.Hour))
	tk := mint(t, priv, Claims{Sandbox: "sb-1", Iat: now.Unix(), Exp: now.Unix() + 30, Jti: "j1", Kid: "k1"})
	c, err := v.Peek(tk, "sb-1", now)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := v.Peek(tk, "sb-1", now); err != nil {
		t.Fatalf("a second peek before any redeem: %v", err)
	}
	if err := v.Redeem(c, now); err != nil {
		t.Fatalf("first redeem: %v", err)
	}
	if err := v.Redeem(c, now); !errors.Is(err, ErrReplayed) {
		t.Fatalf("second redeem: want replayed, got %v", err)
	}
	if _, err := v.Peek(tk, "sb-1", now.Add(time.Second)); !errors.Is(err, ErrReplayed) {
		t.Fatalf("peek after redeem: want replayed, got %v", err)
	}
	if err := v.Redeem(c, now.Add(36*time.Second)); !errors.Is(err, ErrExpired) {
		t.Fatalf("a redeem past the window: want expired, got %v", err)
	}
}

// A spent jti is forgotten once its window closes plus one more lifetime; by then Peek refuses its
// ticket as expired, so forgetting it never reopens a replay - not even after the clock steps back
// by up to MaxLifetime (an NTP correction), which the margin covers.
func TestRedeemPrunesSpentTicketsAfterTheirWindow(t *testing.T) {
	pub, priv := newKey(t)
	now := time.Unix(1_800_000_000, 0)
	v := NewVerifier(map[string]ed25519.PublicKey{"k1": pub}, now.Add(-time.Hour))
	first := Claims{Sandbox: "sb-1", Iat: now.Unix(), Exp: now.Unix() + 30, Jti: "j1", Kid: "k1"}
	if err := v.Redeem(first, now); err != nil {
		t.Fatal(err)
	}
	spent := func(at time.Time, jti string) (bool, int) {
		c := Claims{Sandbox: "sb-1", Iat: at.Unix(), Exp: at.Unix() + 30, Jti: jti, Kid: "k1"}
		if err := v.Redeem(c, at); err != nil {
			t.Fatal(err)
		}
		v.mu.Lock()
		defer v.mu.Unlock()
		_, kept := v.used["j1"]
		return kept, len(v.used)
	}
	if kept, _ := spent(now.Add(36*time.Second), "j2"); !kept {
		t.Fatal("j1 must outlive its window by a lifetime: a clock stepped back would reopen it")
	}
	back := now.Add(36*time.Second - time.Duration(MaxLifetime)*time.Second)
	if _, err := v.Peek(mint(t, priv, first), "sb-1", back); !errors.Is(err, ErrReplayed) {
		t.Fatalf("after a step back the ticket is in its window again and must be replayed, got %v", err)
	}
	if kept, size := spent(now.Add(66*time.Second), "j3"); kept || size != 2 {
		t.Fatalf("j1 should be pruned and j2, j3 kept: kept=%v size=%d", kept, size)
	}
	if _, err := v.Peek(mint(t, priv, first), "sb-1", now.Add(66*time.Second)); !errors.Is(err, ErrExpired) {
		t.Fatalf("a pruned jti's ticket must be expired, got %v", err)
	}
}

func TestOnlyOneConcurrentRedeemWins(t *testing.T) {
	pub, _ := newKey(t)
	now := time.Unix(1_800_000_000, 0)
	v := NewVerifier(map[string]ed25519.PublicKey{"k1": pub}, now.Add(-time.Hour))
	c := Claims{Sandbox: "sb-1", Iat: now.Unix(), Exp: now.Unix() + 30, Jti: "j1", Kid: "k1"}
	var wins atomic.Int32
	var wg sync.WaitGroup
	for range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if v.Redeem(c, now) == nil {
				wins.Add(1)
			}
		}()
	}
	wg.Wait()
	if wins.Load() != 1 {
		t.Fatalf("exactly one redeem may win, got %d", wins.Load())
	}
}

func TestParseKeys(t *testing.T) {
	pub, _ := newKey(t)
	std := base64.StdEncoding.EncodeToString(pub)
	url := base64.RawURLEncoding.EncodeToString(pub)
	keys, err := ParseKeys(" k1:" + std + " , k2:" + url + ",")
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) != 2 || !keys["k1"].Equal(pub) || !keys["k2"].Equal(pub) {
		t.Fatalf("keys: %v", keys)
	}
	if empty, err := ParseKeys(""); err != nil || len(empty) != 0 {
		t.Fatalf("empty spec: %v %v", empty, err)
	}
	short := base64.StdEncoding.EncodeToString(pub[:16])
	for _, bad := range []string{"k1", "k1:", ":" + std, "k1:!!!", "k1:" + short, "k1:" + std + ",k1:" + url} {
		if _, err := ParseKeys(bad); err == nil {
			t.Fatalf("%q must be refused", bad)
		}
	}
}
