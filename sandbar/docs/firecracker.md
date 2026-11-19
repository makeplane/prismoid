# The firecracker backend

Everything about running sandbar's **experimental** backend: what Firecracker needs, how a
sandbox boots, the guest agent, and the security model. It is not a shipping backend yet - no
egress, no display route, no restart or adoption, no automated tier on KVM (README, Support
matrix); the docker backend is the one sandbar ships. If you have never touched Firecracker, read the
[official getting-started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)
first - this doc assumes the concepts (microVM, jailer, vsock) and explains how *sandbar* uses
them.

## What Firecracker gives us

Firecracker is a Virtual Machine Monitor (VMM) built by AWS for Lambda and Fargate. It boots a
minimal Linux guest in a KVM hardware-virtualized VM in ~125 ms with a ~5 MiB memory overhead,
exposing no BIOS, no PCI, no emulated devices beyond virtio-block, virtio-net, and vsock. You
drive one VM through a small REST API over a unix socket.

The security property that matters: **each VM has its own kernel.** Container escapes exploit the
*shared* host kernel; there is no shared kernel here to exploit. This is why AWS runs untrusted
tenant code from millions of Lambda invocations on it, and why it is the right substrate for
running code a language model wrote.

## Host requirements

- **Linux with KVM** - `/dev/kvm` present and accessible. Bare metal or a nested-virt-enabled VM
  (GCP nested virt, AWS `*.metal`, or a local Linux box). **Not macOS** - develop with the docker
  backend there; the firecracker backend refuses to start without `/dev/kvm`.
- **The `firecracker` binary** on `PATH` (or set `SANDBAR_FC_BIN`). Download a release from the
  [Firecracker repo](https://github.com/firecracker-microvm/firecracker/releases).
- **A guest kernel** - an uncompressed `vmlinux` built for Firecracker (virtio, no modules
  needed for our rootfs). The Firecracker CI publishes compatible kernels; point
  `SANDBAR_FC_KERNEL` at it.
- **A rootfs template** - an ext4 image with `sandbar-guestd` inside, built by
  `scripts/build-rootfs.sh` (below). Templates live in `SANDBAR_FC_ROOTFS_DIR` as
  `<image>.ext4`; a spec's `image` selects one.

## How a sandbox boots (`Backend.Create`)

1. **Per-VM dir** under `SANDBAR_FC_RUN_DIR/<id>/` holds the API socket, the vsock socket, a
   private **copy** of the rootfs template (so the template stays pristine), and firecracker's
   log.
2. **Launch** `firecracker --api-sock …` in its own process group, poll for the API socket.
3. **Configure** over the API: boot source (kernel + cmdline), the rootfs drive (read-write),
   machine config (vcpus, memory), and a **vsock device** whose host end is a unix socket in the
   per-VM dir. **No network device is configured** - the VM has no NIC, so there is no route out
   at all.
4. **Start** (`InstanceStart`), then **wait for the guest**: poll the vsock handshake until
   `sandbar-guestd` answers, or the boot timeout trips. This is the honest "the VM is actually
   ready" signal - better than sleeping a fixed guess.

`Destroy` kills the firecracker process (one process = one VM) and removes the per-VM dir.

## The guest agent (`sandbar-guestd`)

Inside every VM runs `sandbar-guestd`, baked into the rootfs and spawned by an init shim. It
listens on `AF_VSOCK` port 52 and serves the `guestwire` protocol: exec, read/write files, list
dir, tar archive/extract. It is the counterpart of the docker backend's Engine-API calls - same
operations, delivered to a process in a VM instead of a container.

**Why vsock and not a network socket:** the VM has no network. vsock is a host↔guest channel that
needs no IP stack, so the daemon can talk to a guest that is otherwise fully isolated. Firecracker
exposes the guest's vsock as a **host unix socket** with a documented handshake: the host connects
to that socket, writes `CONNECT 52\n`, reads `OK <n>\n`, and then has a byte stream to the guest
listener. sandbar implements this handshake directly (`internal/backend/firecracker/vsock.go`),
so the backend stays dependency-free.

The wire framing (`internal/backend/firecracker/call.go` ⇄ `cmd/sandbar-guestd/serve.go`):

```
→  [4-byte big-endian header length][JSON Request][body_len raw bytes]
←  [4-byte big-endian header length][JSON Response][body_len raw bytes]
```

Deliberately the same shape as the public HTTP API - a header plus an optional raw body - so one
mental model spans the whole path.

## Building a rootfs

`scripts/build-rootfs.sh` produces `prismoid-frame.ext4`: a minimal Alpine (or Debian) rootfs
with `sandbar-guestd` at `/sbin/sandbar-guestd`, an init that spawns it, and the frame layout
(`/task /work /artifacts /skills`) pre-created with `/artifacts` owned by uid 1000. Run it on a
Linux host (it needs `mkfs.ext4` and root for the loop mount):

```bash
sudo ./scripts/build-rootfs.sh prismoid-frame
# → /var/lib/sandbar/rootfs/prismoid-frame.ext4
```

To bake your own toolchain in, extend the package list and the `docker export` base at the top of
the script - the same way you would extend a Dockerfile.

## Security model

- **Isolation**: own kernel per VM. A guest kernel exploit does not reach the host or other VMs.
- **No network**: v1 VMs have no NIC. `allow_domains` is carried through the whole path for the
  day egress lands (a virtio-net device + a host allowlist proxy) but enforces nothing now - and
  the code says so, rather than implying a guarantee it does not keep.
- **Workload runs unprivileged**: `guestd` runs commands as uid 1000; only file writes marked
  `owner=system` land as root, and those are read-only inputs by convention. An agent's file write
  and every file read are made as uid 1000 too (one locked OS thread becomes uid 1000, no capabilities;
  the daemon stays root), exactly as on docker: a path the guest's permissions keep from that
  uid answers `403` code `forbidden_path`. A rootfs whose guestd predates this ignores the uid and
  reads and writes as root: rebuild the rootfs (`scripts/build-rootfs.sh`) with the new guestd. A workload can still signal a guestd thread while one of its own operations runs on
  it, and so end its own VM, or prlimit(2) that thread (the one an agent file operation runs on, which guestd discards afterwards) and lower guestd's whole-process limits: a self-inflicted stop or denial of service on an experimental backend, no escalation.
- **Rootfs is a copy**: each VM gets its own disk image; nothing a sandbox does touches the
  template or another VM.
- **Recommended hardening for real deployments**: run firecracker under the
  [jailer](https://github.com/firecracker-microvm/firecracker/blob/main/docs/jailer.md)
  (cgroups + chroot + seccomp + namespaces around the VMM itself), one unprivileged host user per
  VM, and cgroup cpu/io caps. sandbar's `SysProcAttr` detaches the process group; wiring the
  jailer is the next hardening step (roadmap).

## Egress (roadmap - not yet implemented for firecracker)

Today a firecracker sandbox has **no network device at all**, and the backend **refuses**
`allow_domains` rather than run airtight and let a task's fetch fail with no explanation. The
docker backend already does controlled egress (see the README) through a shared allowlist proxy;
this is the design to bring the same to firecracker.

The pieces:

1. **A virtio-net device in the VM config.** Firecracker's `/network-interfaces` API attaches a
   tap device (`host_dev_name`) to the guest as `eth0`. sandbar creates one tap per VM
   (`ip tuntap add`, an address in a per-VM /30), configured before `InstanceStart`.
2. **A locked-down host route.** The tap is NOT bridged to the internet. Host `iptables`/`nftables`
   rules allow the guest to reach **only** the egress proxy's address and drop everything else -
   so even if the guest sets a rogue route, there is nowhere to go but the proxy.
3. **Guest bring-up.** The init shim (`/sbin/sandbar-init`, built by `build-rootfs.sh`) brings
   `eth0` up with the VM's address and a default route to the tap's host side, and exports
   `HTTPS_PROXY=http://sb:<token>@<proxy>` - the same per-sandbox credential the docker backend
   uses, so the proxy keys the allowlist identically. The token is passed to the guest via the
   kernel cmdline or a small config file written into the rootfs copy at create.
4. **Same proxy, same denials.** The guest reaches the shared `internal/egress` proxy; refusals
   land on `GET /v1/sandboxes/{id}/denials` exactly as they do for docker.

Why it is not shipped half-done: tap creation, per-VM addressing, and the host firewall are
Linux-root host operations that cannot be exercised on a developer's macOS machine, and untested
networking code that *looks* finished is worse than an honest "not yet". The interfaces above are
stable; the work is additive behind `Backend.Create`.

## Performance notes

- **Boot** is dominated by kernel + guestd start (~125–200 ms with a lean rootfs), plus the
  rootfs copy. A byte copy of a 200 MiB template is the current cold-start floor; **reflink**
  copies (`FICLONE` on XFS/Btrfs) make it near-instant and are the first optimization (roadmap).
- **Warm pools** - handing out pre-booted VMs - are the single biggest latency win and the
  standard move (E2B, CodeSandbox); additive behind `Backend.Create`.
- **File I/O** over vsock is a memory-to-memory copy through the VMM; fast for the small working
  sets agent tasks produce. Very large artifacts should be streamed (`archive`), not read whole.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| daemon exits: `/dev/kvm not available` | not a KVM host; use the docker backend, or enable nested virt |
| `firecracker binary not found` | put `firecracker` on PATH or set `SANDBAR_FC_BIN` |
| `SANDBAR_FC_KERNEL is required` | set it to an uncompressed vmlinux |
| `guest not ready after 10s` | rootfs missing/broken guestd, or wrong kernel; read `<run_dir>/<id>/firecracker.log` |
| create is slow | rootfs copy cost - enable reflink FS or a warm pool (roadmap) |
