# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Seatbelt fence: the profile every agent command runs under.

`profile_for` writes SBPL text for `sandbox-exec -p`. It starts from `(deny default)` and opens:
reads everywhere minus `READ_DENY` and the Prismoid home (the frame's own folders inside the home are
reopened); writes only in the frame's roots and Prismoid's cache root, minus `PROTECT_WRITE`, the
home's `HOME_PROTECT_WRITE` and the config of every repository that was already there; network
only to loopback (the egress proxy, the agent's own servers) minus Prismoid's own ports; a Unix socket
only for the Docker grant.

SBPL is last-match-wins, so every narrowing rule is emitted after the rule it narrows. Paths are
the real ones (Seatbelt judges the resolved path, and `/tmp`, `/var` are symlinks on macOS); a
denied path is also denied as spelled, in case it is itself a link. One rule per line.
"""

from __future__ import annotations

import os
import pwd
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from prismoid.conf_env import USER_CONFIG
from prismoid.device.fence_data import (
    CACHE_ROOT,
    GIT_WRITABLE_DIRS,
    GIT_WRITABLE_FILES,
    GIT_WRITABLE_PATTERNS,
    HOME_PROTECT_WRITE,
    PROTECT_WRITE,
    READ_DENY,
    READ_DENY_PATTERNS,
)
from prismoid.schemas.device import write_root_refusal

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_REF = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_GIT_PATH = r"/\.git(/|$)"  # a git dir, a gitdir pointer file, and everything inside

# What every toolchain needs from the system under `(deny default)`, each with why. Grown from the
# darwin tests and npm/uv/git/cargo/go smoke runs; prior art is Codex's and Claude Code's profiles.
# launchd takes no job and starts nothing from inside the fence, but `disable` is kept past a reboot
# and it answers it: a person's agent would stay off. Nothing a toolchain runs needs this binary.
NO_LAUNCHCTL: Final = '(deny process-exec (literal "/bin/launchctl"))'
_SYSTEM = f"""\
; Commands run commands; the children inherit this profile.
(allow process-exec)
{NO_LAUNCHCTL}
(allow process-fork)
; Signals only within this sandbox: a command can stop its own children (multiprocessing pools
; hang without it), never the user's processes.
(allow signal (target same-sandbox))
; Python multiprocessing locks and shared memory.
(allow ipc-posix-sem)
(allow ipc-posix-shm)
; User and group lookups (id, getpwuid, ~ expansion).
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
; Network configuration, read-only: Rust HTTP clients (uv, cargo) read the system proxy settings
; and panic without it.
(allow mach-lookup (global-name "com.apple.SystemConfiguration.configd"))
; Certificate trust evaluation: Go and other Security.framework TLS clients verify through trustd.
; Never securityd / SecurityServer: those hand out keychain items, around the Keychains deny.
(allow mach-lookup (global-name "com.apple.trustd.agent"))
; The CPU, memory and OS facts runtimes size themselves by, read-only. The smoke runs need
; hw.machine, kern.hostname, kern.os*, kern.version (node, npm, uv), hw.ncpu (cargo) and
; hw.pagesize_compat (uv, cargo, go); the rest are what Codex's and Claude Code's profiles grant.
(allow sysctl-read
  (sysctl-name "hw.activecpu")
  (sysctl-name "hw.byteorder")
  (sysctl-name "hw.cachelinesize")
  (sysctl-name "hw.cachelinesize_compat")
  (sysctl-name "hw.cpufamily")
  (sysctl-name "hw.cpufrequency")
  (sysctl-name "hw.cpufrequency_compat")
  (sysctl-name "hw.cputype")
  (sysctl-name "hw.l1dcachesize")
  (sysctl-name "hw.l1dcachesize_compat")
  (sysctl-name "hw.l1icachesize")
  (sysctl-name "hw.l1icachesize_compat")
  (sysctl-name "hw.l2cachesize")
  (sysctl-name "hw.l2cachesize_compat")
  (sysctl-name "hw.l3cachesize_compat")
  (sysctl-name "hw.logicalcpu")
  (sysctl-name "hw.logicalcpu_max")
  (sysctl-name "hw.machine")
  (sysctl-name "hw.memsize")
  (sysctl-name "hw.model")
  (sysctl-name "hw.ncpu")
  (sysctl-name "hw.nperflevels")
  (sysctl-name "hw.packages")
  (sysctl-name "hw.pagesize")
  (sysctl-name "hw.pagesize_compat")
  (sysctl-name "hw.physicalcpu")
  (sysctl-name "hw.physicalcpu_max")
  (sysctl-name "hw.tbfrequency_compat")
  (sysctl-name "hw.vectorunit")
  (sysctl-name "kern.argmax")
  (sysctl-name "kern.hostname")
  (sysctl-name "kern.maxfiles")
  (sysctl-name "kern.maxfilesperproc")
  (sysctl-name "kern.maxproc")
  (sysctl-name "kern.ngroups")
  (sysctl-name "kern.osproductversion")
  (sysctl-name "kern.osrelease")
  (sysctl-name "kern.ostype")
  (sysctl-name "kern.osvariant_status")
  (sysctl-name "kern.osversion")
  (sysctl-name "kern.secure_kernel")
  (sysctl-name "kern.usrstack64")
  (sysctl-name "kern.version")
  (sysctl-name "machdep.cpu.brand_string")
  (sysctl-name "machdep.ptrauth_enabled")
  (sysctl-name "sysctl.proc_cputype")
  (sysctl-name "vm.loadavg")
  (sysctl-name-prefix "hw.optional.")
  (sysctl-name-prefix "hw.perflevel")
  (sysctl-name-prefix "machdep.cpu."))
; Output thrown away (npm, go and `id` write to /dev/null).
(allow file-write-data (literal "/dev/null"))
; `> /dev/stderr` and `>(cmd)` reopen a descriptor the command already holds: no new authority.
; (/dev/stdout and /dev/stderr resolve to /dev/fd/1 and /dev/fd/2.)
(allow file-write-data (regex "^/dev/fd/[0-9]+$"))"""


@dataclass(frozen=True)
class FenceSpec:
    write_roots: tuple[str, ...]
    read_only_roots: tuple[str, ...]
    proxy_port: int
    deny_ports: tuple[int, ...]
    docker_socket: str | None = None
    # The `.git` of each repository already in a write root when the frame was made (a folder, or a
    # worktree's pointer file): its config and hooks stay read-only. Repositories the agent makes
    # are its own.
    protected_git: tuple[str, ...] = ()
    # More of them, past what `protected_git`'s rules can bear (each costs regexes the sandbox
    # compiles at every command: 16 take 0.3 s, 64 take 19 s, some 100 no longer fit an argv).
    # Only pinned: its parents cannot be renamed away, and the user-root `.git` rule keeps it
    # read-only, its state files too.
    pinned_git: tuple[str, ...] = ()
    # A granted worktree's own git folder and its repository's, outside every write root (its
    # `.git` pointer names them): read-only but for git's state files, as `protected_git` is.
    linked_git: tuple[str, ...] = ()
    # The folders the user granted (a subset of `write_roots`). The rest are Prismoid's own (frame
    # folders, scratch, TMPDIR) and must sit in the Prismoid home. Only Prismoid's own roots get new repos.
    user_roots: tuple[str, ...] = ()
    # The frame's own cache folder (`cache_root(ref)`), the only cache it writes.
    cache_root: str | None = None

    def __post_init__(self) -> None:
        for port in (self.proxy_port, *self.deny_ports):
            if not 0 < port < 65536:
                raise ValueError(f"not a TCP port: {port}")
        if self.proxy_port in self.deny_ports:
            raise ValueError(f"the egress proxy's port {self.proxy_port} is a denied port")


def sbpl_string(text: str) -> str:
    """`text` as an SBPL string literal. A control character (a newline could end the rule) is
    refused, not escaped: no real path has one."""
    if _CONTROL.search(text):
        raise ValueError(f"path has a control character: {text!r}")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _regex_literal(text: str) -> str:
    """`text` matched literally in an SBPL regex: only the metacharacters are escaped."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text)


def _rule(verb: str, ops: str, kind: str, values: Iterable[str]) -> list[str]:
    return [f"({verb} {ops} ({kind} {sbpl_string(v)}))" for v in values]


def _expand(entry: str, user_home: str) -> str:
    return user_home + entry[1:] if entry == "~" or entry.startswith("~/") else entry


def _spellings(paths: Iterable[str]) -> list[str]:
    """Each path as given (made absolute) and resolved, in order, once."""
    out: list[str] = []
    for path in paths:
        for spelling in (os.path.abspath(path), os.path.realpath(path)):
            if spelling not in out:
                out.append(spelling)
    return out


def _real(paths: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(os.path.realpath(p) for p in paths))


def under(path: str, folder: str) -> bool:
    """Ignoring case: macOS volumes and Seatbelt are case-insensitive (`~/library` IS `~/Library`),
    so a case-sensitive test here could be dodged; erring towards "inside" fails closed."""
    path, folder = path.casefold(), folder.casefold()
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _ancestors(path: str) -> list[str]:
    """Every folder above `path`, nearest first, `/` excluded."""
    out, parent = [], os.path.dirname(path)
    while parent not in ("/", ""):
        out.append(parent)
        parent = os.path.dirname(parent)
    return out


def _between(root: str, top: str) -> list[str]:
    """The folders from `top` down to just above `root`."""
    return [a for a in _ancestors(root) if under(a, top)][::-1]


def _loopback(verb: str, op: str, side: str, port: int | str) -> str:
    return f'({verb} {op} ({side} ip4 "localhost:{port}") ({side} ip6 "localhost:{port}"))'


def _symlinked_files(folders: Iterable[str]) -> list[str]:
    """The targets of symlinked files directly inside `folders` (Seatbelt judges the target)."""
    out: list[str] = []
    for folder in folders:
        try:
            entries = list(os.scandir(folder))
        except OSError:  # missing, or a folder macOS privacy keeps from the helper too
            continue
        for entry in entries:
            target = os.path.realpath(entry.path)
            if entry.is_symlink() and os.path.isfile(target):
                out.append(target)
    return out


def _git_rules(git: str) -> list[str]:
    """A pre-existing repository's git dir (or pointer file): read-only but for everyday git."""
    rules = _rule("deny", "file-write*", "subpath", [git])
    if os.path.isdir(git):
        rules += _rule("allow", "file-write*", "subpath", [f"{git}/{d}" for d in GIT_WRITABLE_DIRS])
        files = [f"{git}/{f}{lock}" for f in GIT_WRITABLE_FILES for lock in ("", ".lock")]
        rules += _rule("allow", "file-write*", "literal", files)
        names = [f"^{_regex_literal(git)}/{p}$" for p in GIT_WRITABLE_PATTERNS]
        rules += _rule("allow", "file-write*", "regex", names)
    return rules


def _user_home(user_home: str | None) -> str:
    return os.path.realpath(user_home or pwd.getpwuid(os.getuid()).pw_dir)


def read_denied(*, user_home: str | None = None) -> list[str]:
    """`READ_DENY` as real paths (each as spelled and resolved), plus the targets of the symlinked
    files directly inside them: what no read or write ever reaches."""
    denied = home_paths((*READ_DENY, USER_CONFIG), user_home=user_home)
    return denied + [t for t in _symlinked_files(denied) if t not in denied]


def home_paths(entries: Iterable[str], *, user_home: str | None = None) -> list[str]:
    """`~/...` entries of a fence list as absolute paths, each as spelled and resolved."""
    home = _user_home(user_home)
    return _spellings(_expand(e, home) for e in entries)


def home_patterns(entries: Iterable[str], *, user_home: str | None = None) -> list[str]:
    """`~/<regex>` entries (`READ_DENY_PATTERNS`) as regexes over a whole path."""
    home = _user_home(user_home)
    return [f"^{_regex_literal(home)}{p[1:]}(/|$)" for p in entries]


def cache_root(ref: str, *, user_home: str | None = None) -> str:
    """The frame `ref`'s own cache folder, under `CACHE_ROOT`."""
    if not _REF.fullmatch(ref):
        raise ValueError(f"not a frame ref: {ref!r}")
    return os.path.join(os.path.realpath(_expand(CACHE_ROOT, _user_home(user_home))), ref)


def alias_root(ref: str, *, user_home: str | None = None) -> str:
    """Where the frame `ref`'s root aliases live (`DeviceFrameState.aliases`): under `CACHE_ROOT`,
    which no frame writes but its own folder, and a frame's ref is never `roots` (`frames.ref_for`)."""
    return os.path.join(os.path.dirname(cache_root(ref, user_home=user_home)), "roots", ref)


def linked_git_refusal(git: str, home: Path | str, *, user_home: str | None = None) -> str | None:
    """Why the fence will not let git write in a granted worktree's git folder `git` (its own, or
    its repository's), or None - the one rule, asked by the fence at every command and by the
    server and the helper when the grant is made. Its reason names no path: it reaches a person's
    error and, at worst, a step's record."""
    user_home = _user_home(user_home)
    real = os.path.realpath(git)
    if write_root_refusal(real, user_home):
        return "lies where Prismoid never lets anything write (outside the home, a hidden or system folder, ~/Library)"
    denied = (*read_denied(user_home=user_home), *_spellings([str(home)]))
    if any(under(real, d) or under(d, real) for d in denied):
        return "overlaps a folder of secrets or Prismoid's own data"
    if any(under(real, d) or under(d, real) for d in home_paths(HOME_PROTECT_WRITE, user_home=user_home)):
        return "overlaps a folder that runs code later (shell, editor or git config)"
    return None


def _vet_frame_roots(roots: Iterable[str], secrets: Sequence[str], patterns: Sequence[str]) -> None:
    for root in roots:
        if any(under(root, s) for s in secrets) or any(re.search(p, root) for p in patterns):
            raise ValueError(f"a frame root inside a denied folder: {root}")


def _vet_user_roots(users: Sequence[str], writes: Sequence[str], user_home: str) -> None:
    for root in users:
        if not any(under(root, w) and under(w, root) for w in writes):
            raise ValueError(f"a user root that is not a write root: {root}")
        if refusal := write_root_refusal(root, user_home):
            raise ValueError(refusal)


def _vet_prismoid_roots(
    writes: Sequence[str], reads: Sequence[str], users: Sequence[str], real_home: str
) -> None:
    for root in writes:
        if root not in users and not under(root, real_home):
            raise ValueError(f"a Prismoid write root outside the Prismoid home: {root}")
    for root in (*writes, *reads):
        if under(real_home, root) and root not in users:
            raise ValueError(f"a frame root that is the Prismoid home: {root}")


def _vet_no_links(spec: FenceSpec) -> None:
    """A write root is the folder it was when the frame took it, and each profile is written from
    the paths as they are now: followed, a root re-aimed behind the sandbox's back would carry the
    frame's write access to wherever it points. A grant was resolved once (`DeviceFrameState.pins`),
    so nothing at all on its path may be a link now."""
    for root in spec.write_roots:
        if os.path.islink(root):
            raise ValueError(f"a write root that is now a link: {root}")
    for root in spec.user_roots:
        if os.path.realpath(root) != os.path.abspath(root):
            raise ValueError(f"a granted folder reached through a link: {root}")


def _vet_linked_git(linked: Sequence[str], writes: Sequence[str], home: Path, user_home: str) -> None:
    for git in linked:
        if any(under(git, w) or under(w, git) for w in writes):
            raise ValueError("a granted worktree's git folder overlaps a write root")
        if why := linked_git_refusal(git, home, user_home=user_home):
            raise ValueError(f"a granted worktree's git folder {why}")


def _vet_caches(caches: Sequence[str], denied: Sequence[str], user_home: str) -> None:
    base = os.path.realpath(_expand(CACHE_ROOT, user_home))
    for cache in caches:
        if not under(cache, base) or under(base, cache):
            raise ValueError(f"a frame cache outside {CACHE_ROOT}: {cache}")
    for cache in caches:
        if any(under(cache, d) or under(d, cache) for d in denied):
            raise ValueError(f"a toolchain cache overlaps a denied folder: {cache}")


@dataclass(frozen=True)
class _Walls:
    """What a profile's file rules name, every path real and vetted."""

    prismoid_home: list[str]
    secrets: list[str]
    patterns: list[str]
    protected: list[str]
    movable: list[str]
    writes: list[str]
    inside: list[str]  # of the frame's folders, those in the Prismoid home: only those reopen
    lookups: list[str]
    caches: list[str]
    users: list[str]
    gits: list[str]
    linked: list[str]
    frozen: list[str]  # the read-only roots inside a write root, which `writes` would otherwise open


def _read_rules(w: _Walls) -> list[str]:
    lines = ["; Reading: everywhere but the Prismoid home and the secrets; the frame's folders reopen."]
    lines += ["(allow file-read*)"]
    lines += _rule("deny", "file-read*", "subpath", w.prismoid_home)
    lines += _rule("allow", "file-read-metadata", "literal", w.lookups)  # walk down, never list
    lines += _rule("allow", "file-read*", "subpath", w.inside)
    lines += _rule("deny", "file-read*", "subpath", w.secrets)
    lines += _rule("deny", "file-read*", "regex", w.patterns)
    return lines


def _write_rules(w: _Walls) -> list[str]:
    lines = ["; Writing: the frame's roots and its own cache folder, never a denied folder."]
    lines += _rule("allow", "file-write*", "subpath", [*w.writes, *w.caches])
    lines += _rule("deny", "file-write*", "subpath", w.prismoid_home)
    lines += _rule("allow", "file-write*", "subpath", [r for r in w.writes if r in w.inside])
    lines += ["; In a user's folder the agent makes no git dir or pointer (git finds the nearest one,"]
    lines += ["; and its config runs in the user's prompt and editor); new repos live in Prismoid's roots."]
    # This stops direct creation only: a repo planted by rename, symlink or bare-repo shape is
    # caught by the helper's plant scan of the user roots (its tests cover it).
    git_path = sbpl_string(_GIT_PATH)
    lines += [
        f"(deny file-write* (require-all (subpath {sbpl_string(u)}) (regex {git_path})))" for u in w.users
    ]
    lines += ["; A repository that was already here keeps its config, hooks and pointers."]
    lines += [rule for git in w.gits for rule in _git_rules(git)]
    if w.linked:
        lines += ["; A granted worktree's git folders, outside the grant: git's state files, nothing else."]
        lines += [rule for git in w.linked for rule in _git_rules(git)]
    lines += _rule("deny", "file-write*", "subpath", w.secrets)
    lines += _rule("deny", "file-write*", "regex", w.patterns)
    lines += ["; Code that runs later, outside the sandbox, is never written."]
    lines += _rule("deny", "file-write*", "regex", PROTECT_WRITE)
    lines += _rule("deny", "file-write*", "subpath", w.protected)
    lines += ["; A folder granted read-only stays so inside one granted read-write."]
    lines += _rule("deny", "file-write*", "subpath", w.frozen)
    lines += ["; A denied or protected path cannot be moved out from under its rule by renaming a parent,"]
    lines += ["; and a root stays where it is: `subpath` lets its own entry go, and a link in its place"]
    lines += ["; would be the next command's fence."]
    lines += _rule("deny", "file-write-unlink", "literal", w.movable)
    return lines


def _network_rules(spec: FenceSpec) -> list[str]:
    lines = ["; Network: loopback only (the egress proxy, the agent's own servers), never Prismoid's ports."]
    lines += ["; Wide loopback is ADR-078's call; a non-loopback listener is the helper's to kill."]
    lines += ["; Seatbelt's `localhost` also matches a wildcard bind (0.0.0.0): a server the agent starts"]
    lines += ["; is reachable as if the user had started it, but nothing here can dial out but loopback."]
    lines += [_loopback("allow", "network-bind network-inbound", "local", "*")]
    lines += [_loopback("allow", "network-outbound", "remote", "*")]
    for port in spec.deny_ports:
        lines += [_loopback("deny", "network-bind", "local", port)]
        lines += [_loopback("deny", "network-outbound", "remote", port)]
    lines += ["; The egress proxy: dialled, never squatted."]
    lines += [_loopback("deny", "network-bind", "local", spec.proxy_port)]
    lines += [_loopback("allow", "network-outbound", "remote", spec.proxy_port)]
    if spec.docker_socket:
        lines += ["; The Docker grant: the helper's filtering Docker socket, and no other Unix socket."]
        lines += [
            f"(allow network-outbound (remote unix-socket (path-literal {sbpl_string(p)})))"
            for p in _spellings([spec.docker_socket])
        ]
    return lines


def profile_for(spec: FenceSpec, home: Path, *, user_home: str | None = None) -> str:
    """The SBPL profile for one command of a frame whose Prismoid home is `home`, or a `ValueError` for a
    frame it would not be safe to write one for. Each root is vetted as it is read, in this order.
    In the profile the last matching rule wins, so the order of the rules is the fence too.
    `user_home` is a seam for tests; the default is the real home, never the command's `$HOME`."""
    user_home = _user_home(user_home)
    _vet_no_links(spec)
    prismoid_home = _spellings([str(home)])
    real_home = os.path.realpath(home)
    secrets = read_denied(user_home=user_home)
    patterns = home_patterns(READ_DENY_PATTERNS, user_home=user_home)
    writes, reads = _real(spec.write_roots), _real(spec.read_only_roots)
    _vet_frame_roots((*writes, *reads), secrets, patterns)
    users = _real(spec.user_roots)
    _vet_user_roots(users, writes, user_home)
    _vet_prismoid_roots(writes, reads, users, real_home)
    gits = _real(spec.protected_git)
    for git in gits:
        if not any(under(git, w) for w in writes):
            raise ValueError(f"a protected git dir outside the write roots: {git}")
    pins = _real(spec.pinned_git)
    for git in pins:
        if not any(under(git, u) for u in users):
            raise ValueError(f"a pinned git dir outside the user roots: {git}")
    linked = sorted(_real(spec.linked_git), key=len)
    _vet_linked_git(linked, writes, home, user_home)
    # The frame's own folders live in the Prismoid home; only those reopen.
    inside = [r for r in (*writes, *reads) if under(r, real_home)]
    lookups = list(dict.fromkeys(a for r in inside for a in _between(r, real_home)))
    caches = _real([spec.cache_root] if spec.cache_root else [])
    _vet_caches(caches, (*secrets, *prismoid_home), user_home)
    protected = home_paths(HOME_PROTECT_WRITE, user_home=user_home)
    roots = (*writes, *reads, *caches)
    held = (
        a
        for p in (*secrets, *prismoid_home, *gits, *pins, *linked, *protected, *roots)
        for a in _ancestors(p)
    )
    movable = dict.fromkeys((*roots, *held))
    walls = _Walls(
        prismoid_home=prismoid_home,
        secrets=secrets,
        patterns=patterns,
        protected=protected,
        movable=list(movable),
        writes=writes,
        inside=inside,
        lookups=lookups,
        caches=caches,
        users=users,
        gits=gits,
        linked=linked,
        frozen=[r for r in reads if any(under(r, w) for w in writes)],
    )
    lines = ["(version 1)", "(deny default)", "", _SYSTEM, "", *_read_rules(walls), ""]
    lines += [*_write_rules(walls), "", *_network_rules(spec)]
    return "\n".join(lines) + "\n"
