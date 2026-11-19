# ai/frames - folder rules
Scope: `backend/prismoid/ai/frames/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## What this folder is for
The sandbox an agent works inside, behind one factory: `new_frame(...)` in `__init__.py` is the one
place a driver is chosen (the `DRIVERS` table, keyed by `FrameDriverName`), and every driver
satisfies the `FrameDriver` Protocol in `driver.py`:
- `docker_driver.py` - a container per frame on the worker's own Docker (the dev default).
- `sandbar_driver.py` - a container per frame through the sandbar daemon's HTTP API.
- `device_driver.py` - the person's own Mac through `prismoid device helper` (ADR-078), with
  `device_client.py` (the helper's Unix-socket door) and `device_tar.py` (a directory artifact
  tarred without following links).

`paper.py` satisfies the Protocol too, in memory, for a step that needs no computer (`new_paper`,
never a deployment's driver, ADR-063); `on_demand.py` is the step's frame that starts as paper and
becomes its Machine at the first call that needs one.

The rest: `layout.py` is the frame's directory contract; `publish.py` the publish hand-off (type
check, static runner, manifest read) and `product.py` the target and manifest entry it writes;
`egress.py` the allowlist side; `archives.py` the checkpoint tar filters; `preview.py` the
in-frame preview runner; `remote_browser.py`, `cdp_ws.py` and `frame_sink.py` the browser driven
from the worker over CDP (the device); `screen.py` and the `*_screen.py` files the
screen upstreams; `image/`, `image_code/` and `image_browser/` the Docker images (a line: base <
code < browser, each `FROM` the one before; their tags and builds are `images.py`). The browser
sidecar (`image_browser/sidecar/`) runs inside the frame and is also the worker's browser library:
`remote_browser.py` drives its verbs and recorder over a worker-side connection.

It is NOT where a Machine's life is decided: only `operations/machines/` calls lifecycle verbs on a
Machine's frame (ADR-063).

## Do
- Callers ask `new_frame` and never name a backend. An existing Machine's frame is reached
  through the driver its row names, never this process's default.
- Artifacts exist only via `frame.publish()`: a file becomes an Artifact by passing through it
  into `/artifacts` with a manifest entry; nothing else is harvestable, so a deliverable has one door.
- An exec-backed driver (docker, sandbar) hands off through the static runner plus JSON
  arguments, never shell interpolation, and reads its manifest with `publish.read_manifest`; the
  device publishes in-process and builds its entry with `product.manifest_entry`. Every driver takes
  its target from `product.artifact_target`.
- A path the frames share is a `Final` constant in `layout.py` with its contract in the
  docstring; no driver redefines one.
- A capability only some drivers have (native sleep, a screen, a browser) is its own Protocol
  that those drivers implement and callers check for (`NativeSleep`, `Checkpoints`,
  `RunsSystemPython`, ...).
- The Protocol's docstrings state the contract, not each driver's quirks; a quirk lives on the
  driver's own method.
- Read settings through `prismoid.ai.config.current()`, never `prismoid.conf`.
- A sandbar Machine has a screen only with `PRISMOID_COMPUTER_USE_SANDBAR_SCREEN` on (ADR-065).
- A driver is one row of `DRIVERS` (a function, so only the driver asked for is imported), plus a
  name in `EARLY_BOOT_DRIVERS` or a module in `CLIENT_MODULES` where it qualifies, and its screen
  door one row of `screen.DOORS` (ADR-103). Code that runs for every driver goes through
  `__init__.py` (`close_clients`, `require_driver`).

## Never
- Copy `publish`, the manifest entry or the exec manifest read into a driver: use `publish.py` and
  `product.py`. The egress-version logic is still each driver's own; a new driver factors it out
  before it adds another copy.
- Raise `NotImplementedError` for a Protocol member a driver cannot honour.
- Compare a driver name as a bare string (`== "docker"`): use `FrameDriverName`.
- Bypass `frame.publish()` to place a file in `/artifacts`.

## Checked by machine
- `.importlinter`: `frames` is an independent sibling (`|`, enforced) of `gateway`, `skills`,
  `tools` and `bots` at the bottom of the engine's layers; the engine is persistence-free; `ai/`
  reads settings only through `prismoid.ai.config`.
- `backend/tools/audit.sh`: "ai/ imports neither db, operations, jobs, nor api", "no URL literals
  outside conf.py / providers / links.py", "no hardcoded provider keys".
- `quality:py.file_lines`, `quality:py.nesting`, `quality:py.positional`,
  `quality:py.complexity`. What is baselined here is in `backend/tools/quality_baseline.json`;
  new code meets the caps.
- `image/house/` is first-party, excluded from the size ratchet.

## Verify
`uv run pytest tests/unit/ai/frames -q`; with Docker, `uv run pytest tests/integration/frames -q`
(`test_house_engine.py` renders on every image profile).

## Write it like this
- `layout.py`: every path a `Final` constant with a docstring stating the contract and the
  reason; every driver leans on it and none copies it.
- `__init__.py`: one factory, a docstring that names each driver and why it exists, callers
  never naming a backend.
- `driver.py`'s `ExecResult` and the error types (`FrameUnreachable`, `ArchiveTooLarge`):
  small frozen values and typed failures a caller can catch by name.

## Review focus
- A diff that touches the same verb in two drivers: is it shared product logic that belongs in
  `publish.py` or `product.py`?
- A Protocol member added for one driver and refused by the others.
- A driver docstring or a Protocol docstring that now narrates another driver's behaviour.
- A lifecycle verb (`bind`, `freeze`, `suspend`, `destroy`) called from here on behalf of a
  Machine, or a frame that holds a resource its `close()` does not release.
- Shell interpolation of anything model-authored on the way into a frame.
