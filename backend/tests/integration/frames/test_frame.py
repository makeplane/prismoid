# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The frame - the computer the agent works inside.

The property that matters is the one that is easiest to lose: **a frame has no route out.** Every
other guarantee in the product is downstream of it. An agent that is *asked* not to call an API
and one that *cannot* are not the same system, and only one of them is safe to run someone else's
prompt in.

Needs Docker and the frame image (`prismoid dev frame-selftest` builds it).
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import uuid
from typing import Any

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.frames.layout import ALL, ARTIFACTS, INPUTS, WORK, input_path, is_publishable

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-a")]


@pytest.fixture
async def frame():
    ensure_image()
    f = DockerFrame(task_id=f"pytest-{uuid.uuid4().hex[:8]}")
    await f.create(allow_domains=[])
    try:
        yield f
    finally:
        await f.destroy()


async def test_a_frame_has_no_route_out(frame: DockerFrame) -> None:
    """THE test. The container sits on an `internal` Docker network - no gateway, no DNS, no
    route. Not a policy the agent is asked to respect; a network it does not have."""
    result = await frame.exec("timeout 5 curl -sS -o /dev/null https://example.com; echo rc=$?")
    assert "rc=0" not in result.stdout, "the frame reached the internet"


async def test_dns_does_not_even_resolve(frame: DockerFrame) -> None:
    """Belt and braces: with no gateway there is nothing to ask, so a name does not resolve.
    An agent cannot exfiltrate to a host it cannot find."""
    result = await frame.exec("timeout 4 getent hosts example.com; echo rc=$?")
    assert "rc=0" not in result.stdout


async def test_the_layout_exists(frame: DockerFrame) -> None:
    result = await frame.exec(f"ls -d {' '.join(ALL)}")
    assert sorted(result.stdout.split()) == sorted(ALL)


async def test_the_agent_is_not_root(frame: DockerFrame) -> None:
    """An agent with root inside its sandbox is one container escape from being root outside."""
    result = await frame.exec("id -u")
    assert result.stdout.strip() != "0"


async def test_the_toolchain_is_in_the_image(frame: DockerFrame) -> None:
    """Pinned in the image, because a frame that can `pip install` is a frame with a way out."""
    result = await frame.exec("python -c 'import openpyxl, docx, pptx, pandas, matplotlib, reportlab, pypdf'")
    assert result.ok, result.stderr


async def test_the_code_tools_are_on_the_code_image_and_only_there(frame: DockerFrame) -> None:
    """A code step's prompt says it has git, rg and node; the code image keeps that promise, and a
    step on the base image (every step that makes no code) does not carry them."""
    smoke = "git --version && rg --version && node -v && npm -v"
    assert not (await frame.exec(smoke)).ok
    ensure_image("code")
    code = DockerFrame(task_id=f"pytest-{uuid.uuid4().hex[:8]}", profile="code")
    await code.create(allow_domains=[])
    try:
        result = await code.exec(f"{smoke} && node -e 'console.log(6 * 7)' && id -u")
        assert result.ok, result.stderr
        assert result.stdout.split()[-2:] == ["42", "1000"]  # node runs, as the agent, not root
        assert (await code.exec("python -c 'import openpyxl, docx, pandas'")).ok  # the base toolchain stays
    finally:
        await code.destroy()


async def test_a_checkpoint_unpacks_into_a_different_frame(frame: DockerFrame) -> None:
    """This is why a Machine survives its frame - a wake unpacks the last
    checkpoint of `/work` instead of re-running the work that built it."""
    await frame.write_file(f"{WORK}/memo.md", "# Memo\n\nverification beats vibes.\n")
    checkpoint = io.BytesIO()
    await frame.archive(WORK, checkpoint)
    assert checkpoint.tell() > 0

    other = DockerFrame(task_id=f"pytest-restore-{uuid.uuid4().hex[:8]}")
    await other.create(allow_domains=[])
    try:
        checkpoint.seek(0)
        await other.unpack(WORK, checkpoint)
        recovered = await other.read_file(f"{WORK}/memo.md")
        assert "verification beats vibes" in recovered
    finally:
        await other.destroy()


async def test_a_checkpoint_preserves_a_symlink(frame: DockerFrame) -> None:
    """The old `_archive_sync` called `extractfile()` on every tar member unconditionally - a
    symlink has no content to extract, and Chromium's profile is what first exposed the crash
    (`KeyError: linkname ... not found`). A symlink written by an agent's own shell is just as
    real, on any frame - not only a browser one - and must survive the checkpoint as a symlink."""
    await frame.exec("ln -s /usr/bin/python3 /work/link")
    checkpoint = io.BytesIO()
    await frame.archive(WORK, checkpoint)
    checkpoint.seek(0)
    with tarfile.open(fileobj=checkpoint, mode="r") as tar:
        member = tar.getmember("work/link")
        assert member.issym()


async def test_a_checkpoint_does_not_drop_a_file_merely_named_singleton(frame: DockerFrame) -> None:
    """The `Singleton*` filter that keeps Chromium's stale process-lock symlinks out of a
    restored browser profile must never reach outside `/browser` - an agent's own
    `Singleton.java` is source code, not a lock file, and belongs in every checkpoint."""
    await frame.write_file(f"{WORK}/Singleton.java", "class Singleton {}\n")
    checkpoint = io.BytesIO()
    await frame.archive(WORK, checkpoint)
    checkpoint.seek(0)
    with tarfile.open(fileobj=checkpoint, mode="r") as tar:
        assert "work/Singleton.java" in tar.getnames()


def test_only_artifacts_is_publishable() -> None:
    """/work is a workshop; /artifacts is a loading dock. The asymmetry is the whole design."""
    assert is_publishable(f"{ARTIFACTS}/memo.md")
    assert not is_publishable(f"{WORK}/memo.md")
    assert not is_publishable("/task/plan.md")


# ── the sources are the system's, not the agent's ─────────────────────────────────────────────


TRUTH = b"Revenue was $4.2M in Q3.\n"


async def test_the_agent_can_read_its_sources(frame: DockerFrame) -> None:
    await frame.write_bytes(input_path("brief.md"), TRUTH, mode=0o444, owner="system")
    assert "4.2M" in await frame.read_file(input_path("brief.md"))


async def test_the_agent_can_edit_its_own_work(frame: DockerFrame) -> None:
    """The control, and it is not a formality - it failed the first time it was written.

    Every file the system copies in is stamped with the uid in the tar member, and `TarInfo`
    defaults to **root**. So `/work` files written by the system were root-owned, and the agent
    could not edit its own scratch with a shell: write `build.py`, run a formatter over it, get
    `Permission denied`. Nothing had noticed, because nothing had tried.

    It also guards the *next* test from a trap - a refusal is only evidence that a lock
    works if the same shell, the same syntax, the same session can be shown to succeed where it is
    allowed to. Without this, "all eight attacks failed" is equally consistent with "exec is broken".
    """
    mine = f"{WORK}/mine.md"
    await frame.write_bytes(mine, TRUTH)
    assert (await frame.exec(f"echo tampered > {mine}")).ok, "the agent cannot edit its own workshop"
    assert "tampered" in await frame.read_file(mine)


async def test_a_read_only_mode_is_not_enough_ownership_is(frame: DockerFrame) -> None:
    """**Why `owner="system"` exists**, and it is the subtlety that would be silently refactored away.

    The obvious lock is the mode: write the source 0444 and it cannot be written. That is false, and
    it is false in the direction that matters. Permission *bits* do not govern `chmod` - **ownership
    does** - so a 0444 file the agent owns is one `chmod 0644` away from being rewritten. The read
    -only bit is a note to a co-operative process, not a lock against an adversarial one.

    So the source is root's. This test pins the reason, by showing the same 0444 file falls over the
    moment the agent owns it - which is exactly what a future "simplify the frame driver" would do.
    """
    soft = f"{WORK}/agent-owned.md"
    await frame.write_bytes(soft, TRUTH, mode=0o444, owner="agent")

    assert not (await frame.exec(f"echo 'Revenue was $9.9M' > {soft}")).ok  # 0444 blocks the write…
    assert (await frame.exec(f"chmod 0644 {soft}")).ok  # …but the owner may simply remove the bit
    assert (await frame.exec(f"echo 'Revenue was $9.9M' > {soft}")).ok

    assert "9.9M" in await frame.read_file(soft), "a mode alone did not protect the file - as designed"


async def test_the_agent_cannot_edit_its_sources(frame: DockerFrame) -> None:
    """**THE test of M2.** Everything the verifier claims rests on this holding.

    The document suite asks "does this number appear in a source?". If the agent can *write* to a
    source, the answer is always yes: invent the figure, append it to `/task/inputs/brief.md`, and
    the grounding check finds it there and passes. Verification becomes a mirror.

    Three locks, and every one of them is load-bearing:
      * the file is **root's** - so the agent cannot `chmod` its way in (see the test above);
      * the file is **0444** - so not even a mistake writes to it;
      * `/task` **and** `/task/inputs` are root's and 0555 - because deleting an entry needs write
        permission on the directory *containing* it, so an agent that owned `/task` could simply
        `mv` the inputs directory aside and build a friendlier one. The lock has to be on the
        parent, or it is not a lock.
    """
    source = input_path("brief.md")
    await frame.write_bytes(source, TRUTH, mode=0o444, owner="system")  # exactly as execute_step does

    for attempt in (
        f"echo 'Revenue was $9.9M' > {source}",  # overwrite
        f"echo 'Revenue was $9.9M' >> {source}",  # append
        f"rm -f {source}",  # delete the file
        f"mv {source} {WORK}/stolen.md",  # rename it out
        f"chmod 0644 {source}",  # take the read-only bit off
        f"chown agent {source}",  # take the file
        f"rm -rf {INPUTS}",  # delete the directory
        f"mv {INPUTS} /task/inputs.old && mkdir -p {INPUTS}",  # replace the directory wholesale
        "mv /task /task.old",  # replace the parent wholesale
        f"ln -sf /work/lies.md {source}",  # swap it for a symlink to something writable
    ):
        result = await frame.exec(attempt)
        assert not result.ok, f"the sandbox ALLOWED: {attempt}"

    survived = await frame.read_file(source)
    assert survived == TRUTH.decode(), f"an agent rewrote its own source: {survived!r}"


async def test_no_exec_runs_a_startup_file_the_agent_planted(frame: DockerFrame) -> None:
    """HOME is `/work`, the agent's own, and the system's commands (the manifest read, a file
    read, the recording copy, a skill's mkdir) run as the agent's uid through the same `exec` its
    shell does. A startup file the agent leaves in HOME would run inside every one of them: hook
    `cp` there and the recording the engine publishes is the agent's forgery. So no exec reads
    one - not even the agent's own shell, which a login shell gave nothing it uses. Root's own
    commands (a source's mkdir, a Machine's mount reset) inherit that HOME unless told otherwise,
    so they run with root's: not even a login shell there could reach the agent's file."""
    hook = 'echo "$BASH_EXECUTION_STRING" >> /tmp/hooked\n'
    for startup in (".profile", ".bash_profile", ".bashrc"):  # what a login sh/bash or an rc reads
        await frame.write_file(f"{WORK}/{startup}", hook)  # exactly as the agent's write_file tool

    await frame.exec(f"mkdir -p {WORK}/rec && cp -r {WORK}/rec {WORK}/rec-copy")
    await frame.manifest()
    await frame.read_file(f"{WORK}/.bash_profile")
    await frame.list_dir(WORK)
    await frame.write_bytes(input_path("src.md"), b"source", mode=0o444, owner="system")
    await frame.reset_mounts()

    ran = (await frame.exec("cat /tmp/hooked 2>/dev/null; true")).stdout
    assert ran == "", f"a startup file the agent planted ran inside:\n{ran}"
    home = frame._exec_as(0, ["printenv", "HOME"]).output  # pyright: ignore[reportPrivateUsage]
    assert home == b"/root\n", f"a root command ran with HOME={home!r}"


async def test_a_hostile_path_is_read_and_listed_and_runs_nothing(frame: DockerFrame) -> None:
    """A model-chosen path with quotes, `$(...)`, backticks or newlines is one argv word, never shell."""
    names = ("x'$(touch /work/pwned)", 'x"$(touch /work/pwned)"', "x`touch /work/pwned`", "a b", "two\nlines")
    for name in names:
        await frame.write_file(f"{WORK}/{name}.md", name)
        assert await frame.read_file(f"{WORK}/{name}.md") == name
        await frame.write_file(f"{WORK}/{name}/f.md", name)
        assert await frame.list_dir(f"{WORK}/{name}") == ["f.md"]
    with pytest.raises(FileNotFoundError):
        await frame.read_file(f"{WORK}/missing'$(touch /work/pwned)")
    assert await frame.list_dir(f"{WORK}/missing'$(touch /work/pwned)") == []
    assert "pwned" not in await frame.list_dir(WORK), "a path ran as a command"


async def test_a_command_past_its_bound_is_killed_with_all_it_started(frame: DockerFrame) -> None:
    """A stuck command is stopped in the frame, its background children too, not merely left
    running behind a caller that stopped waiting; the result says it timed out."""
    result = await frame.exec("sleep 997 & sleep 998", timeout=2)

    assert result.exit_code == 124 and "timed out after 2s; killed" in result.stderr
    left = await frame.exec(r"cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\0' ' '; true")
    assert "sleep 99" not in left.stdout, f"a timed-out command still runs: {left.stdout}"


async def test_read_bytes_round_trips_binary(frame: DockerFrame) -> None:
    """The bug M5 owed: `read_file` is `cat` and mangles non-UTF-8 bytes, so a harvested `.xlsx`
    hashed to something other than what was published. `read_bytes` is byte-exact - the same tar
    transport as `write_bytes` - so a binary artifact survives the round trip out of the frame."""
    blob = bytes(range(256)) * 8  # every byte value, including ones no UTF-8 decode survives
    await frame.write_bytes("/work/blob.bin", blob, owner="agent")

    out = await frame.read_bytes("/work/blob.bin")
    assert out == blob, "read_bytes must return the exact bytes written"
    assert hashlib.sha256(out).hexdigest() == hashlib.sha256(blob).hexdigest()

    # And prove the contrast: read_file (cat → str → utf-8) does NOT survive arbitrary bytes.
    as_text = await frame.read_file("/work/blob.bin")
    assert as_text.encode() != blob, "read_file corrupts binary - that is exactly why read_bytes exists"


async def test_a_symlink_the_agent_made_cannot_carry_write_file_into_task(frame: DockerFrame) -> None:
    """`put_archive` extracts as root and follows a link inside the container, so the agent's own
    `ln -s /task /work/t` would carry `write_file('/work/t/context.md')` into `/task`. The tool
    resolves the target in the frame and refuses; the driver alone would not have."""
    from prismoid.ai.tools.frame_tools import frame_toolset

    async def journal(*_args: object, **_kwargs: object) -> None: ...

    class _Ctx:
        tool_call_id = "toolu_symlink"

    await frame.exec(f"ln -s /task {WORK}/t && ln -s /verify/suite.py {WORK}/notes.md")
    write: Any = frame_toolset(("frame.write_file",), frame, journal).tools["write_file"].function
    for path in (f"{WORK}/t/context.md", f"{WORK}/notes.md"):
        result = await write(_Ctx(), path, "planted")
        assert str(result).startswith("Refused:"), result
    landed = await frame.exec("cat /task/context.md /verify/suite.py 2>/dev/null; true")
    assert "planted" not in landed.stdout, "a write followed the agent's link out of /work"

    await frame.exec(f"mkdir -p {WORK}/runs/3 && ln -s {WORK}/runs/3 {WORK}/latest")
    assert str(await write(_Ctx(), f"{WORK}/latest/out.md", "kept")).startswith("wrote ")
    assert await frame.read_file(f"{WORK}/runs/3/out.md") == "kept"
