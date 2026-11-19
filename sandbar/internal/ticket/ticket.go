// Copyright (c) 2026-present Plane Software, Inc. and contributors
// SPDX-License-Identifier: AGPL-3.0-only
// See the LICENSE file for details.

// Package ticket verifies display tickets, the one credential sandbar's display route accepts.
// The Plane API signs them with its screen key; sandbar holds only
// public keys, by kid, so a sandbar compromise cannot mint one. A ticket is single-use and lives
// at most 30 seconds:
//
//	dt_<base64url(claims JSON)>.<base64url(Ed25519 signature)>   base64url without padding
//	claims: {"sandbox", "iat", "exp", "jti", "kid"}              iat/exp in unix seconds
//	signed: "sandbar-display-v1\n" + the claims bytes, exactly as they travel
//
// Peek checks a ticket without consuming it (before the upgrade); Redeem consumes it (after). The
// jti cache lives in memory, so a restart empties it - which is why a ticket issued before the
// verifier started plus the skew is refused: a restart cannot reopen the replay window.
package ticket

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"
)

const (
	// Prefix marks a display ticket. The API's screen tickets (st_) are signed under another
	// domain, so neither kind verifies as the other.
	Prefix = "dt_"
	// Domain is prepended to the claims bytes before signing.
	Domain = "sandbar-display-v1\n"
	// MaxLifetime is the longest exp - iat a ticket may carry, in seconds.
	MaxLifetime int64 = 30
	// Skew is the clock difference tolerated either way between the API and sandbar, in seconds.
	Skew int64 = 5
	// maxLen bounds what is parsed at all; a real ticket is about 250 bytes.
	maxLen = 2048
)

// The refusals, by the names the API's own minter uses (computer/operations/tickets.py). The two
// sides may name an edge differently (a ticket dated ahead of this clock is expired here, lifetime
// there) and check in another order; every one of them refuses.
var (
	ErrMalformed  = errors.New("malformed")
	ErrUnknownKid = errors.New("unknown_kid")
	ErrSignature  = errors.New("signature")
	ErrLifetime   = errors.New("lifetime")
	ErrExpired    = errors.New("expired")
	ErrStale      = errors.New("stale")
	ErrMismatch   = errors.New("mismatch")
	ErrReplayed   = errors.New("replayed")
)

// Claims is what a ticket says: which sandbox it opens, its window, its one-use id, and the key.
type Claims struct {
	Sandbox string `json:"sandbox"`
	Iat     int64  `json:"iat"`
	Exp     int64  `json:"exp"`
	Jti     string `json:"jti"`
	Kid     string `json:"kid"`
}

var b64 = base64.RawURLEncoding.Strict()

// ParseKeys reads SANDBAR_DISPLAY_KEYS: "kid:base64,kid:base64", each the raw 32-byte Ed25519
// public key in standard or URL-safe base64, padded or not. Empty means no keys (the display
// route is then off). Any malformed entry is an error, so a typo stops the daemon at boot.
func ParseKeys(spec string) (map[string]ed25519.PublicKey, error) {
	keys := map[string]ed25519.PublicKey{}
	for _, item := range strings.Split(spec, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		kid, enc, ok := strings.Cut(item, ":")
		kid, enc = strings.TrimSpace(kid), strings.TrimSpace(enc)
		if !ok || kid == "" || enc == "" {
			return nil, fmt.Errorf("SANDBAR_DISPLAY_KEYS: an entry is not kid:base64")
		}
		if _, dup := keys[kid]; dup {
			return nil, fmt.Errorf("SANDBAR_DISPLAY_KEYS: kid %q appears twice", kid)
		}
		raw, err := decodeKey(enc)
		if err != nil || len(raw) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("SANDBAR_DISPLAY_KEYS: kid %q is not the base64 of a 32-byte Ed25519 public key", kid)
		}
		keys[kid] = ed25519.PublicKey(raw)
	}
	return keys, nil
}

// decodeKey takes either base64 alphabet, padded or not: operators paste keys from wherever the
// API printed them.
func decodeKey(s string) ([]byte, error) {
	s = strings.TrimRight(s, "=")
	if strings.ContainsAny(s, "-_") {
		return base64.RawURLEncoding.DecodeString(s)
	}
	return base64.RawStdEncoding.DecodeString(s)
}

// Verifier checks and redeems display tickets. Safe for concurrent use.
type Verifier struct {
	keys    map[string]ed25519.PublicKey
	started int64 // unix seconds; a ticket issued before started + Skew is refused

	mu   sync.Mutex
	used map[string]int64 // jti -> exp, kept a lifetime past exp + Skew (see Redeem)
}

// NewVerifier verifies with keys, by kid. started is when this process began.
func NewVerifier(keys map[string]ed25519.PublicKey, started time.Time) *Verifier {
	return &Verifier{keys: keys, started: started.Unix(), used: map[string]int64{}}
}

// Peek checks a ticket for sandbox at now without consuming it: format, key, signature,
// lifetime, window, start, sandbox, and that its jti is unused.
func (v *Verifier) Peek(ticket, sandbox string, now time.Time) (Claims, error) {
	c, err := v.verify(ticket)
	if err != nil {
		return Claims{}, err
	}
	t := now.Unix()
	switch {
	case c.Exp <= c.Iat || c.Exp-c.Iat > MaxLifetime:
		return Claims{}, ErrLifetime
	case t > c.Exp+Skew || c.Iat > t+Skew:
		return Claims{}, ErrExpired
	case c.Iat < v.started+Skew:
		// A minting clock up to Skew ahead stamps a ticket the previous process may have spent
		// with an iat up to Skew past this start (the API's own rule).
		return Claims{}, ErrStale
	case c.Sandbox != sandbox:
		return Claims{}, ErrMismatch
	}
	v.mu.Lock()
	_, spent := v.used[c.Jti]
	v.mu.Unlock()
	if spent {
		return Claims{}, ErrReplayed
	}
	return c, nil
}

// Redeem consumes a peeked ticket: the first Redeem of a jti wins, every later one is
// ErrReplayed. It prunes spent jtis a lifetime after their window closed: Peek refuses them as
// expired from exp + Skew on, and the extra MaxLifetime keeps a clock stepped back that far (an
// NTP correction) from finding a forgotten jti inside its window again.
func (v *Verifier) Redeem(c Claims, now time.Time) error {
	t := now.Unix()
	v.mu.Lock()
	defer v.mu.Unlock()
	for jti, exp := range v.used {
		if t > exp+Skew+MaxLifetime {
			delete(v.used, jti)
		}
	}
	if t > c.Exp+Skew {
		return ErrExpired
	}
	if _, spent := v.used[c.Jti]; spent {
		return ErrReplayed
	}
	v.used[c.Jti] = c.Exp
	return nil
}

// verify parses a ticket and checks its signature with the key its kid names.
func (v *Verifier) verify(ticket string) (Claims, error) {
	if len(ticket) > maxLen {
		return Claims{}, ErrMalformed
	}
	body, ok := strings.CutPrefix(ticket, Prefix)
	if !ok {
		return Claims{}, ErrMalformed
	}
	claimsPart, sigPart, ok := strings.Cut(body, ".")
	if !ok || strings.Contains(sigPart, ".") {
		return Claims{}, ErrMalformed
	}
	raw, err := b64.DecodeString(claimsPart)
	if err != nil {
		return Claims{}, ErrMalformed
	}
	sig, err := b64.DecodeString(sigPart)
	if err != nil || len(sig) != ed25519.SignatureSize {
		return Claims{}, ErrMalformed
	}
	c, err := parseClaims(raw)
	if err != nil {
		return Claims{}, ErrMalformed
	}
	key, ok := v.keys[c.Kid]
	if !ok {
		return Claims{}, ErrUnknownKid
	}
	if !ed25519.Verify(key, append([]byte(Domain), raw...), sig) {
		return Claims{}, ErrSignature
	}
	return c, nil
}

// parseClaims is strict: exactly one JSON object, no unknown field, every claim present.
func parseClaims(raw []byte) (Claims, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var c Claims
	if err := dec.Decode(&c); err != nil {
		return Claims{}, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return Claims{}, errors.New("trailing data after the claims")
	}
	if c.Sandbox == "" || c.Jti == "" || c.Kid == "" || c.Iat <= 0 || c.Exp <= 0 {
		return Claims{}, errors.New("a claim is missing")
	}
	return c, nil
}
